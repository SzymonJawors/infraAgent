import json
import asyncio
import io
import queue
import re
import threading
import zipfile
from difflib import unified_diff
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import RedirectResponse, Response, StreamingResponse

from app.core.config import Settings, get_settings
from app.api.schemas import (
    ApprovalRequest,
    AgentJobStatus,
    CommandBody,
    ConversationMessage,
    ConversationResponse,
    FileBody,
    FilesResponse,
    MessageRequest,
    MessageResponse,
    RenameSessionRequest,
    PlanSummary,
    ResourceAction,
    SessionSummary,
    SessionsResponse,
    SessionStatus,
    ValidationBody,
    FileChange,
    OperationHistory,
    OperationRecord,
    ErrorAnalysisBody,
)
from app.services.agent import AgentCancelled, AgentTurn, InfraAgent
from app.services.approval import (
    clear_receipts,
    drop_receipt,
    matching_receipt,
    read_receipt,
    write_receipt,
)
from app.services.scanner import WorkspaceScanner
from app.services.sessions import (
    delete_session,
    finish_operation,
    get_operation,
    list_operations,
    list_sessions,
    rename_session,
    require_session_id,
    session_dir,
    start_operation,
    touch_session,
    update_operation_output,
)
from app.services.terraform import TerraformService
from app.services.pricing import AwsPricing
from app.services.workspace_lock import WorkspaceBusyError, workspace_lease
from app.services.error_analysis import analyze_terraform_error
from app.services.agent_context import read_conversation
from app.services.agent_context import detect_region, provider_region, write_conversation
from app.services.github import (
    GithubError,
    authorization_url,
    exchange_code,
    list_repositories,
    new_state,
    analyze_repository,
    terraform_files,
)

router = APIRouter()
_JOB_STATUS_FILE = ".agent-job.json"
_JOB_LOCK = threading.Lock()
_JOB_EVENTS: dict[str, threading.Event] = {}
_OPERATION_RESULTS: dict[tuple[str, int], CommandBody] = {}
_OPERATION_RESULTS_LOCK = threading.Lock()
_ACTIVE_SERVICES: dict[tuple[str, int], TerraformService] = {}
_GITHUB_STATES: dict[str, str] = {}
_GITHUB_TOKENS: dict[str, str] = {}


def _job_status_path(workspace: Path) -> Path:
    return workspace / _JOB_STATUS_FILE


def _write_job_status(workspace, status: AgentJobStatus) -> None:
    target = _job_status_path(workspace)
    temporary = target.with_suffix(".tmp")
    temporary.write_text(status.model_dump_json(), encoding="utf-8")
    temporary.replace(target)


def _read_job_status(workspace) -> AgentJobStatus:
    try:
        return AgentJobStatus.model_validate_json(
            _job_status_path(workspace).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return AgentJobStatus()


def _run_agent(
    agent: InfraAgent,
    message: str,
    publish,
    cancel_event: threading.Event,
    skip_checkov: bool = False,
) -> AgentTurn:
    try:
        return agent.run(
            message,
            on_progress=publish,
            cancel_event=cancel_event,
            skip_checkov=skip_checkov,
        )
    except TypeError as exc:
        if "unexpected keyword argument" not in str(exc):
            raise
        return agent.run(message, on_progress=publish)


def _region_clarification(workspace: Path, message: str) -> MessageResponse | None:
    existing = WorkspaceScanner(workspace).read()
    if detect_region(message) or provider_region(existing):
        return None
    reply = (
        "Which AWS region should I create the infrastructure in? "
        "For example: `eu-central-1` (Frankfurt) or `us-east-1`."
    )
    write_conversation(
        workspace / ".agent-conversation.json",
        [
            *read_conversation(workspace / ".agent-conversation.json"),
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply},
        ],
    )
    empty = ValidationBody(ok=True, missing_binary=False, output="")
    return MessageResponse(
        reply=reply,
        files=WorkspaceScanner(workspace).files(),
        validation=empty,
        plan=empty,
        scan=empty,
        registry_docs=False,
    )


@router.get("/sessions", response_model=SessionsResponse)
def get_sessions() -> SessionsResponse:
    return SessionsResponse(sessions=list_sessions())


@router.post("/sessions/{session_id}", response_model=SessionSummary, status_code=201)
def create_session(session_id: str) -> SessionSummary:
    workspace = _workspace_or_400(session_id, create=True)
    del workspace
    session = next(item for item in list_sessions() if item["id"] == session_id)
    return SessionSummary(**session)


@router.get("/sessions/{session_id}/status", response_model=SessionStatus)
def get_session_status(session_id: str) -> SessionStatus:
    workspace = _workspace_or_400(session_id)
    state_path = workspace / "terraform.tfstate"
    if not state_path.is_file():
        return SessionStatus(status="not_started", terraform_state=False)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        resources = [
            resource for resource in state.get("resources", [])
            if resource.get("mode") == "managed"
            and resource.get("instances")
        ]
    except (OSError, json.JSONDecodeError, AttributeError):
        return SessionStatus(status="state_unreadable", terraform_state=True)
    return SessionStatus(
        status="applied" if resources else "empty",
        terraform_state=True,
        managed_resources=len(resources),
    )


@router.get("/sessions/{session_id}/conversation", response_model=ConversationResponse)
def get_conversation(session_id: str) -> ConversationResponse:
    workspace = _workspace_or_400(session_id)
    messages = read_conversation(workspace / ".agent-conversation.json")
    return ConversationResponse(
        messages=[ConversationMessage(**message) for message in messages]
    )


@router.get("/github/auth")
def github_auth(session_id: str) -> RedirectResponse:
    settings = get_settings()
    if not settings.github_client_id or not settings.github_client_secret:
        raise HTTPException(status_code=503, detail="GitHub OAuth is not configured")
    _workspace_or_400(session_id, create=True)
    state = new_state()
    _GITHUB_STATES[state] = session_id
    return RedirectResponse(
        authorization_url(settings.github_client_id, settings.github_redirect_uri, state)
    )


@router.get("/github/callback")
def github_callback(code: str, state: str) -> RedirectResponse:
    session_id = _GITHUB_STATES.pop(state, None)
    settings = get_settings()
    if not session_id:
        raise HTTPException(status_code=400, detail="Invalid or expired GitHub OAuth state")
    try:
        _GITHUB_TOKENS[session_id] = exchange_code(
            settings.github_client_id,
            settings.github_client_secret,
            code,
            settings.github_redirect_uri,
        )
    except GithubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    return RedirectResponse(f"{settings.github_frontend_url}/?github=connected")


@router.get("/sessions/{session_id}/github/status")
def github_status(session_id: str) -> dict[str, bool]:
    _workspace_or_400(session_id)
    return {"connected": session_id in _GITHUB_TOKENS}


@router.get("/sessions/{session_id}/github/repositories")
def github_repositories(session_id: str) -> list[dict[str, object]]:
    _workspace_or_400(session_id)
    token = _GITHUB_TOKENS.get(session_id)
    if not token:
        raise HTTPException(status_code=401, detail="Connect GitHub first")
    try:
        return [repository.__dict__ for repository in list_repositories(token)]
    except GithubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/sessions/{session_id}/github/import")
def import_github_terraform(session_id: str, body: dict[str, str]) -> dict[str, object]:
    workspace = _workspace_or_400(session_id)
    token = _GITHUB_TOKENS.get(session_id)
    full_name = body.get("full_name", "")
    branch = body.get("default_branch", "main")
    if not token:
        raise HTTPException(status_code=401, detail="Connect GitHub first")
    try:
        files = terraform_files(token, full_name, branch)
    except GithubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    if not files:
        raise HTTPException(status_code=404, detail="No Terraform files found in this repository")
    for path, content in files.items():
        target = (workspace / path).resolve()
        if workspace.resolve() not in target.parents or target.suffix != ".tf":
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    clear_receipts(workspace)
    return {"repository": full_name, "branch": branch, "files": sorted(files)}


@router.post("/sessions/{session_id}/github/analyze")
def analyze_github_repository(session_id: str, body: dict[str, str]) -> dict[str, object]:
    workspace = _workspace_or_400(session_id)
    token = _GITHUB_TOKENS.get(session_id)
    full_name = body.get("full_name", "")
    branch = body.get("default_branch", "main")
    if not token:
        raise HTTPException(status_code=401, detail="Connect GitHub first")
    if not full_name or not branch:
        raise HTTPException(status_code=400, detail="Repository and branch are required")
    try:
        analysis = analyze_repository(token, full_name, branch)
    except GithubError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    try:
        rename_session(session_id, full_name)
    except (ValueError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="Could not link repository to session") from exc
    (workspace / ".github-repository-analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=True, indent=2),
        encoding="utf-8",
    )
    return analysis


@router.get("/sessions/{session_id}/agent-status", response_model=AgentJobStatus)
def get_agent_status(session_id: str) -> AgentJobStatus:
    return _read_job_status(_workspace_or_400(session_id))


@router.post("/sessions/{session_id}/agent-cancel", response_model=AgentJobStatus)
def cancel_agent(session_id: str) -> AgentJobStatus:
    workspace = _workspace_or_400(session_id)
    with _JOB_LOCK:
        event = _JOB_EVENTS.get(session_id)
        if event is None:
            return _read_job_status(workspace)
        event.set()
    current = _read_job_status(workspace)
    cancelled = current.model_copy(update={"status": "cancelling"})
    _write_job_status(workspace, cancelled)
    return cancelled


@router.get("/sessions/{session_id}/operations", response_model=OperationHistory)
def get_operations(session_id: str) -> OperationHistory:
    _workspace_or_400(session_id)
    return OperationHistory(
        operations=[OperationRecord(**item) for item in list_operations(session_id)]
    )


@router.get("/sessions/{session_id}/operations/{operation_id}", response_model=CommandBody)
def get_operation_result(session_id: str, operation_id: int) -> CommandBody:
    _workspace_or_400(session_id)
    with _OPERATION_RESULTS_LOCK:
        result = _OPERATION_RESULTS.get((session_id, operation_id))
    if result is not None:
        return result
    operation = next(
        (item for item in list_operations(session_id) if item["id"] == operation_id),
        None,
    )
    if operation is None:
        raise HTTPException(status_code=404, detail="Operation not found")
    if operation["status"] == "running":
        return CommandBody(
            ok=True,
            command=f"terraform {operation['kind']}",
            output=str(operation.get("output") or "Terraform operation is still running."),
            status="running",
            operation_id=operation_id,
        )
    return CommandBody(
        ok=operation["ok"] if operation["ok"] is not None else operation["status"] == "completed",
        command=str(operation["command"] or f"terraform {operation['kind']}"),
        output=str(operation["output"] or operation["summary"] or "Terraform operation finished."),
        status="completed" if operation["status"] == "completed" else "failed",
        operation_id=operation_id,
    )


@router.delete("/sessions/{session_id}", status_code=204)
def remove_session(session_id: str) -> None:
    try:
        require_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session id") from exc
    try:
        delete_session(session_id)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Could not delete the session") from exc


@router.patch("/sessions/{session_id}", response_model=SessionSummary)
def rename_session_route(session_id: str, body: RenameSessionRequest) -> SessionSummary:
    try:
        updated = rename_session(session_id, body.title)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Session not found") from exc
    return SessionSummary(**updated)


@router.post("/sessions/{session_id}/messages", response_model=MessageResponse)
def post_message(session_id: str, body: MessageRequest) -> MessageResponse:
    try:
        require_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session id") from exc

    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured")

    workspace = session_dir(session_id)
    touch_session(session_id)
    clarification = _region_clarification(workspace, body.message)
    if clarification:
        return clarification
    try:
        with workspace_lease(workspace):
            before = _workspace_snapshot(workspace)
            clear_receipts(workspace)
            turn = InfraAgent(workspace, settings).run(
                body.message,
                skip_checkov=body.skip_checkov,
            )
            if turn.plan.ok and turn.scan.ok:
                write_receipt(
                    workspace,
                    "apply",
                    plan_output=turn.plan.full_output(),
                    session_id=session_id,
                )
            response = _response(turn, _workspace_changes(workspace, before))
    except WorkspaceBusyError as exc:
        raise HTTPException(
            status_code=409,
            detail="This workspace is already processing another operation. Wait for it to finish or restart the backend if no operation is active.",
        ) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_detail(exc, settings)) from exc
    return response


@router.post("/sessions/{session_id}/messages/stream")
def post_message_stream(session_id: str, body: MessageRequest) -> StreamingResponse:
    try:
        require_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session id") from exc

    settings = get_settings()
    if not settings.openai_api_key:
        raise HTTPException(status_code=503, detail="OpenAI API key is not configured")

    workspace = session_dir(session_id)
    before = _workspace_snapshot(workspace)
    touch_session(session_id)
    clarification = _region_clarification(workspace, body.message)
    if clarification:
        events: queue.Queue[tuple[str, dict[str, object]] | None] = queue.Queue()
        events.put(("result", clarification.model_dump()))
        events.put(None)
        return StreamingResponse(
            _stream_events(events),
            media_type="text/event-stream",
        )
    clear_receipts(workspace)
    _write_job_status(workspace, AgentJobStatus(status="running"))
    cancel_event = threading.Event()
    operation_id = start_operation(session_id, "agent")
    with _JOB_LOCK:
        if session_id in _JOB_EVENTS:
            raise HTTPException(status_code=409, detail="An agent job is already running")
        _JOB_EVENTS[session_id] = cancel_event
    events: queue.Queue[tuple[str, dict[str, object]] | None] = queue.Queue()

    def publish(stage: str, message: str, command: str | None = None) -> None:
        payload: dict[str, object] = {"stage": stage, "message": message}
        if command:
            payload["command"] = command
        events.put(("status", payload))
        current = _read_job_status(workspace)
        _write_job_status(
            workspace,
            current.model_copy(update={
                "status": "running",
                "progress": [*current.progress, {"stage": stage, "message": message}],
            }),
        )

    def run_agent() -> None:
        try:
            turn = _run_agent(
                InfraAgent(workspace, settings),
                body.message,
                publish,
                cancel_event,
                body.skip_checkov,
            )
            if turn.plan.ok and turn.scan.ok:
                write_receipt(
                    workspace,
                    "apply",
                    plan_output=turn.plan.full_output(),
                    session_id=session_id,
                )
            response = _response(turn, _workspace_changes(workspace, before))
            _write_job_status(workspace, AgentJobStatus(status="completed", result=response))
            finish_operation(session_id, operation_id, "completed", response.reply)
            events.put(("result", response.model_dump()))
        except AgentCancelled as exc:
            message = str(exc)
            _write_job_status(workspace, AgentJobStatus(status="cancelled", error=message))
            finish_operation(session_id, operation_id, "cancelled", message)
            events.put(("error", {"message": message}))
        except Exception as exc:
            error = _safe_detail(exc, settings)
            _write_job_status(workspace, AgentJobStatus(status="failed", error=error))
            finish_operation(session_id, operation_id, "failed", error)
            events.put(("error", {"message": error}))
        finally:
            with _JOB_LOCK:
                _JOB_EVENTS.pop(session_id, None)
            events.put(None)

    threading.Thread(target=run_agent, daemon=True).start()

    return StreamingResponse(
        _stream_events(events),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/sessions/{session_id}/messages/ws")
async def post_message_websocket(websocket: WebSocket, session_id: str) -> None:
    try:
        require_session_id(session_id)
    except ValueError:
        await websocket.close(code=1008, reason="Invalid session id")
        return
    settings = get_settings()
    if not settings.openai_api_key:
        await websocket.close(code=1011, reason="OpenAI API key is not configured")
        return
    await websocket.accept()
    try:
        payload = json.loads(await websocket.receive_text())
        body = MessageRequest.model_validate(payload)
    except (json.JSONDecodeError, ValueError):
        await websocket.close(code=1003, reason="Invalid message payload")
        return
    workspace = session_dir(session_id)
    before = _workspace_snapshot(workspace)
    touch_session(session_id)
    clarification = _region_clarification(workspace, body.message)
    if clarification:
        await websocket.send_json({"type": "result", "data": clarification.model_dump()})
        await websocket.close()
        return
    clear_receipts(workspace)
    cancel_event = threading.Event()
    with _JOB_LOCK:
        if session_id in _JOB_EVENTS:
            await websocket.send_json({"type": "error", "data": {"message": "An agent job is already running."}})
            await websocket.close(code=1013)
            return
        _JOB_EVENTS[session_id] = cancel_event
    _write_job_status(workspace, AgentJobStatus(status="running"))
    operation_id = start_operation(session_id, "agent")
    events: queue.Queue[tuple[str, dict[str, object]] | None] = queue.Queue()

    def publish(stage: str, message: str, command: str | None = None) -> None:
        data: dict[str, object] = {"stage": stage, "message": message}
        if command:
            data["command"] = command
        events.put(("status", data))
        current = _read_job_status(workspace)
        _write_job_status(workspace, current.model_copy(update={
            "status": "running",
            "progress": [*current.progress, {"stage": stage, "message": message}],
        }))

    def run_agent() -> None:
        try:
            turn = _run_agent(
                InfraAgent(workspace, settings),
                body.message,
                publish,
                cancel_event,
                body.skip_checkov,
            )
            if turn.plan.ok and turn.scan.ok:
                write_receipt(
                    workspace,
                    "apply",
                    plan_output=turn.plan.full_output(),
                    session_id=session_id,
                )
            response = _response(turn, _workspace_changes(workspace, before))
            _write_job_status(workspace, AgentJobStatus(status="completed", result=response))
            finish_operation(session_id, operation_id, "completed", response.reply)
            events.put(("result", response.model_dump()))
        except AgentCancelled as exc:
            error = str(exc)
            _write_job_status(workspace, AgentJobStatus(status="cancelled", error=error))
            finish_operation(session_id, operation_id, "cancelled", error)
            events.put(("error", {"message": error}))
        except WorkspaceBusyError:
            error = (
                "This workspace is already processing another operation. "
                "Wait for it to finish before sending another request."
            )
            _write_job_status(workspace, AgentJobStatus(status="failed", error=error))
            finish_operation(session_id, operation_id, "failed", error)
            events.put(("error", {"message": error}))
        except Exception as exc:
            error = _safe_detail(exc, settings)
            _write_job_status(workspace, AgentJobStatus(status="failed", error=error))
            finish_operation(session_id, operation_id, "failed", error)
            events.put(("error", {"message": error}))
        finally:
            with _JOB_LOCK:
                _JOB_EVENTS.pop(session_id, None)
            events.put(None)

    threading.Thread(target=run_agent, daemon=True).start()
    try:
        while True:
            try:
                incoming = await asyncio.wait_for(websocket.receive_text(), timeout=0.1)
                if incoming.strip().lower() == "cancel":
                    cancel_event.set()
            except asyncio.TimeoutError:
                pass
            except WebSocketDisconnect:
                return
            try:
                event = events.get_nowait()
            except queue.Empty:
                continue
            if event is None:
                return
            event_type, data = event
            await websocket.send_json({"type": event_type, "data": data})
    finally:
        if not cancel_event.is_set():
            # Disconnecting the viewer must not cancel the backend job.
            pass


@router.get("/sessions/{session_id}/files", response_model=FilesResponse)
def get_files(session_id: str) -> FilesResponse:
    workspace = _workspace_or_400(session_id)
    scanner = WorkspaceScanner(workspace)
    return FilesResponse(
        files=[
            FileBody(name=name, content=(workspace / name).read_text(encoding="utf-8"))
            for name in scanner.files()
        ]
    )


@router.get("/sessions/{session_id}/export")
def export_session(session_id: str) -> Response:
    workspace = _workspace_or_400(session_id)
    terraform_files = WorkspaceScanner(workspace).files()
    if not terraform_files:
        raise HTTPException(status_code=404, detail="No Terraform files to export")

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for name in terraform_files:
            bundle.write(workspace / name, arcname=name)
        bundle.writestr(
            "README.md",
            (
                "# Terraform project\n\n"
                "Generated by InfraAgent. Review the Terraform plan and configure "
                "your AWS credentials before running Terraform locally.\n\n"
                "```powershell\n"
                "terraform init\n"
                "terraform plan\n"
                "```\n"
            ),
        )
        if (workspace / ".terraform.lock.hcl").is_file():
            bundle.write(workspace / ".terraform.lock.hcl", arcname=".terraform.lock.hcl")

    archive.seek(0)
    filename = f"infraagent-{session_id}.zip"
    return Response(
        content=archive.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/sessions/{session_id}/plans/destroy", response_model=CommandBody)
def post_destroy_plan(session_id: str) -> CommandBody:
    workspace = _workspace_or_400(session_id, create=True)
    settings = get_settings()
    if not _has_aws(settings):
        drop_receipt(workspace, "destroy")
        raise HTTPException(status_code=503, detail="AWS credentials are not configured")
    operation_id = start_operation(session_id, "destroy-plan")
    output_buffer: list[str] = []

    def publish(line: str, command: str) -> None:
        output_buffer.append(line)
        update_operation_output(session_id, operation_id, "\n".join(output_buffer), command=command)

    service = TerraformService(workspace, settings, on_output=publish)
    _ACTIVE_SERVICES[(session_id, operation_id)] = service
    completed = threading.Event()
    result_holder: dict[str, CommandBody] = {}

    def run_plan() -> None:
        try:
            result = service.plan_destroy()
            if result.ok and _destroy_plan_has_changes(result.full_output()):
                write_receipt(
                    workspace,
                    "destroy",
                    plan_output=result.full_output(),
                    session_id=session_id,
                )
            else:
                drop_receipt(workspace, "destroy")
            final_body = _command_body(result, operation_id)
            cancelled = (get_operation(session_id, operation_id) or {}).get("status") == "cancelled"
            if cancelled:
                final_body = final_body.model_copy(
                    update={"ok": False, "status": "cancelled", "output": "Terraform operation cancelled."}
                )
            finish_operation(
                session_id,
                operation_id,
                "cancelled" if cancelled else ("completed" if result.ok else "failed"),
                "Cancellation requested." if cancelled else final_body.status,
                command=final_body.command,
                output=final_body.output,
                ok=final_body.ok,
            )
        except Exception as exc:
            detail = _safe_detail(exc, settings)
            cancelled = (get_operation(session_id, operation_id) or {}).get("status") == "cancelled"
            finish_operation(session_id, operation_id, "cancelled" if cancelled else "failed", detail)
            final_body = CommandBody(
                ok=False,
                command="terraform plan -destroy",
                output=detail,
                status="cancelled" if cancelled else "failed",
                operation_id=operation_id,
            )
        with _OPERATION_RESULTS_LOCK:
            _OPERATION_RESULTS[(session_id, operation_id)] = final_body
        _ACTIVE_SERVICES.pop((session_id, operation_id), None)
        result_holder["body"] = final_body
        completed.set()

    threading.Thread(target=run_plan, daemon=True).start()
    if completed.wait(0.05):
        return result_holder["body"]
    return CommandBody(
        ok=True,
        command="terraform plan -destroy",
        output="Terraform destroy preview started.",
        status="running",
        operation_id=operation_id,
    )


@router.post("/sessions/{session_id}/apply", response_model=CommandBody)
def post_apply(session_id: str, body: ApprovalRequest) -> CommandBody:
    return _run_approved(session_id, body, "apply")


@router.post("/sessions/{session_id}/plans/apply", response_model=CommandBody)
def post_apply_plan(session_id: str) -> CommandBody:
    workspace = _workspace_or_400(session_id)
    receipt = read_receipt(workspace, "apply")
    if receipt is None:
        raise HTTPException(status_code=409, detail="No matching apply plan for the current files")
    output = receipt.get("plan_output", "")
    return CommandBody(
        ok=True,
        command="terraform plan",
        output=output or "The reviewed apply plan is ready.",
        status="completed",
        plan_summary=_plan_summary(output),
    )


@router.post("/sessions/{session_id}/destroy", response_model=CommandBody)
def post_destroy(session_id: str, body: ApprovalRequest) -> CommandBody:
    return _run_approved(session_id, body, "destroy")


def _response(turn: AgentTurn, changes: list[FileChange] | None = None) -> MessageResponse:
    summary = _plan_summary(_redact(turn.plan.full_output()))
    if turn.plan.ok:
        estimate = AwsPricing(get_settings()).estimate(_redact(turn.plan.full_output()))
        summary = summary.model_copy(update={
            "estimated_monthly_usd": estimate.monthly_usd,
            "pricing_status": estimate.status,
            "pricing_items": estimate.items,
        })
    return MessageResponse(
        reply=turn.reply,
        files=turn.files,
        validation=ValidationBody(
            ok=turn.validation.ok,
            missing_binary=turn.validation.missing_binary,
            output=_redact(turn.validation.full_output()),
            policy_warnings=turn.validation.policy_warnings,
        ),
        plan=ValidationBody(
            ok=turn.plan.ok,
            missing_binary=turn.plan.missing_binary,
            output=_redact(turn.plan.full_output()),
            policy_warnings=turn.plan.policy_warnings,
        ),
        scan=ValidationBody(
            ok=turn.scan.ok,
            missing_binary=turn.scan.missing_binary,
            output=_redact(turn.scan.full_output()),
            policy_warnings=turn.scan.policy_warnings,
        ),
        registry_docs=turn.registry_docs,
        plan_summary=summary,
        changes=changes or [],
    )


def _workspace_snapshot(workspace) -> dict[str, str]:
    scanner = WorkspaceScanner(workspace)
    return {name: (workspace / name).read_text(encoding="utf-8") for name in scanner.files()}


def _workspace_changes(workspace, before: dict[str, str]) -> list[FileChange]:
    after = _workspace_snapshot(workspace)
    changes: list[FileChange] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name, "")
        new = after.get(name, "")
        if old == new:
            continue
        status = "added" if name not in before else "deleted" if name not in after else "modified"
        diff = "".join(unified_diff(
            old.splitlines(keepends=True),
            new.splitlines(keepends=True),
            fromfile=f"a/{name}",
            tofile=f"b/{name}",
        ))
        changes.append(FileChange(name=name, status=status, diff=diff))
    return changes


def _stream_events(
    events: queue.Queue[tuple[str, dict[str, object]] | None],
) -> Iterator[str]:
    while True:
        event = events.get()
        if event is None:
            return
        event_type, payload = event
        yield f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def _redact(text: str) -> str:
    settings = get_settings()
    for secret in (
        settings.openai_api_key,
        settings.aws_secret_access_key,
        settings.aws_access_key_id,
    ):
        if secret:
            text = text.replace(secret, "[redacted]")
    return text


def _safe_detail(exc: Exception, settings: Settings) -> str:
    text = str(exc)
    if settings.openai_api_key:
        text = text.replace(settings.openai_api_key, "[redacted]")
    if settings.aws_secret_access_key:
        text = text.replace(settings.aws_secret_access_key, "[redacted]")
    if settings.aws_access_key_id:
        text = text.replace(settings.aws_access_key_id, "[redacted]")
    text = " ".join(text.split())
    if len(text) > 500:
        text = text[:500] + "..."
    return text or "Agent request failed"


def _workspace_or_400(session_id: str, *, create: bool = False):
    try:
        require_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session id") from exc
    workspace = session_dir(session_id, create=create)
    if create:
        touch_session(session_id)
    return workspace


def _has_aws(settings: Settings) -> bool:
    return bool(settings.aws_access_key_id and settings.aws_secret_access_key)


def _destroy_plan_has_changes(output: str) -> bool:
    normalized = " ".join(output.lower().split())
    if "no changes. no objects need to be destroyed" in normalized:
        return False
    if re.search(r"#\s+[^\s]+\s+will be destroyed", output, re.IGNORECASE):
        return True
    match = re.search(r"plan:\s*(\d+)\s+to add,\s*(\d+)\s+to change,\s*(\d+)\s+to destroy", normalized)
    if match:
        return int(match.group(3)) > 0
    return True


def _run_approved(session_id: str, body: ApprovalRequest, kind: str) -> CommandBody:
    if body.approve is not True:
        raise HTTPException(status_code=400, detail="Approval is required")
    workspace = _workspace_or_400(session_id, create=True)
    settings = get_settings()
    if not _has_aws(settings):
        raise HTTPException(status_code=503, detail="AWS credentials are not configured")
    if not matching_receipt(workspace, kind):
        raise HTTPException(
            status_code=409,
            detail=f"No matching {kind} plan for the current files",
        )
    operation_id = start_operation(session_id, kind)
    output_buffer: list[str] = []

    def publish(line: str, command: str) -> None:
        output_buffer.append(line)
        update_operation_output(session_id, operation_id, "\n".join(output_buffer), command=command)

    service = TerraformService(workspace, settings, on_output=publish)
    _ACTIVE_SERVICES[(session_id, operation_id)] = service

    def run_operation() -> None:
        try:
            result = service.apply() if kind == "apply" else service.destroy()
            if result.ok:
                drop_receipt(workspace, "apply")
                drop_receipt(workspace, "destroy")
            final_body = _command_body(result, operation_id)
            cancelled = (get_operation(session_id, operation_id) or {}).get("status") == "cancelled"
            if cancelled:
                final_body = final_body.model_copy(
                    update={"ok": False, "status": "cancelled", "output": "Terraform operation cancelled."}
                )
            with _OPERATION_RESULTS_LOCK:
                _OPERATION_RESULTS[(session_id, operation_id)] = final_body
            finish_operation(
                session_id,
                operation_id,
                "cancelled" if cancelled else ("completed" if result.ok else "failed"),
                "Cancellation requested." if cancelled else final_body.status,
                command=final_body.command,
                output=final_body.output,
                ok=final_body.ok,
            )
        except Exception as exc:
            detail = _safe_detail(exc, settings)
            cancelled = (get_operation(session_id, operation_id) or {}).get("status") == "cancelled"
            finish_operation(session_id, operation_id, "cancelled" if cancelled else "failed", detail)
            final_body = CommandBody(
                ok=False,
                command=f"terraform {kind}",
                output=detail,
                status="cancelled" if cancelled else "failed",
                operation_id=operation_id,
            )
            with _OPERATION_RESULTS_LOCK:
                _OPERATION_RESULTS[(session_id, operation_id)] = final_body
        _ACTIVE_SERVICES.pop((session_id, operation_id), None)

    threading.Thread(target=run_operation, daemon=True).start()
    return CommandBody(
        ok=True,
        command=f"terraform {kind}",
        output="Terraform operation started. Waiting for AWS/Terraform to finish.",
        status="running",
        operation_id=operation_id,
    )


@router.post("/sessions/{session_id}/operations/{operation_id}/cancel", response_model=OperationRecord)
def cancel_operation(session_id: str, operation_id: int) -> OperationRecord:
    _workspace_or_400(session_id)
    service = _ACTIVE_SERVICES.get((session_id, operation_id))
    if service is None:
        raise HTTPException(status_code=409, detail="Operation is not running")
    service.cancel()
    finish_operation(session_id, operation_id, "cancelled", "Cancellation requested.")
    operation = next(
        item for item in list_operations(session_id) if item["id"] == operation_id
    )
    return OperationRecord(**operation)


def _command_body(result, operation_id: int | None = None) -> CommandBody:
    output = _redact(result.full_output())
    status = "completed"
    is_destroy = _is_destroy_command(result.command)
    if is_destroy and not _destroy_plan_has_changes(output):
        status = "no_managed_resources"
    elif not result.ok:
        status = "failed"
    summary = _plan_summary(output, destroy=is_destroy) if is_destroy else None
    raw_analysis = analyze_terraform_error(output, result.command) if not result.ok else None
    analysis = ErrorAnalysisBody(**raw_analysis.__dict__) if raw_analysis else None
    return CommandBody(
        ok=result.ok,
        command=result.command,
        output=output,
        status=status,
        operation_id=operation_id,
        plan_summary=summary,
        error_analysis=analysis,
    )


def _is_destroy_command(command: str) -> bool:
    return bool(
        re.search(r"(?:^|\s)-{1,2}destroy(?:\s|$)", command)
        or re.search(r"(?:^|\s)terraform\s+destroy(?:\s|$)", command)
    )


def _plan_summary(output: str, *, destroy: bool = False) -> PlanSummary:
    match = re.search(
        r"Plan:\s*(\d+)\s+to add,\s*(\d+)\s+to change,\s*(\d+)\s+to destroy",
        output,
        re.IGNORECASE,
    )
    resources = sorted(set(re.findall(r"#\s+([^\s]+)\s+will be", output)))
    resource_actions = sorted([
        ResourceAction(address=address, action=_resource_action(verb))
        for address, verb in re.findall(
            r"#\s+([^\s]+)\s+will be\s+(created|updated|destroyed|replaced)",
            output,
            re.IGNORECASE,
        )
    ], key=lambda item: item.address)
    destroy_resources = sorted(set(re.findall(
        r"#\s+([^\s]+)\s+will be destroyed",
        output,
        re.IGNORECASE,
    )))
    warnings: list[str] = []
    lower = output.lower()
    if "aws_db_instance." in lower:
        warnings.append("RDS instance: storage, backups, and database runtime incur AWS charges.")
    if "multi_az" in lower:
        warnings.append("Multi-AZ RDS increases database cost because standby capacity is provisioned.")
    if "aws_instance." in lower:
        warnings.append("EC2 instance and EBS storage incur hourly and storage charges.")
    if "aws_cloudwatch_log_group." in lower:
        warnings.append("CloudWatch Logs incur ingestion and storage charges.")
    if "aws_s3_bucket." in lower:
        warnings.append("S3 incurs storage, request, and data transfer charges; lifecycle and retention affect the final bill.")
    if "aws_kms_key." in lower:
        warnings.append("Customer-managed KMS keys incur a monthly key charge plus API request charges.")
    if "aws_nat_gateway." in lower:
        warnings.append("NAT Gateway has hourly and per-GB processing charges.")
    if not match:
        return PlanSummary(
            resources=resources,
            resource_actions=resource_actions,
            cost_warnings=warnings,
        )
    to_destroy = int(match.group(3))
    if destroy and destroy_resources:
        # Terraform can emit a stale/partial summary when refresh output is
        # interleaved with the destroy plan. The explicit resource actions are
        # authoritative for the review shown to the user.
        to_destroy = len(destroy_resources)
        resources = destroy_resources
        resource_actions = [
            ResourceAction(address=address, action="destroy")
            for address in destroy_resources
        ]
    return PlanSummary(
        to_add=int(match.group(1)),
        to_change=int(match.group(2)),
        to_destroy=to_destroy,
        resources=resources,
        resource_actions=resource_actions,
        cost_warnings=warnings,
    )


def _resource_action(verb: str) -> str:
    normalized = verb.lower()
    if normalized == "created":
        return "create"
    if normalized == "destroyed":
        return "destroy"
    if normalized == "replaced":
        return "replace"
    return "change"
