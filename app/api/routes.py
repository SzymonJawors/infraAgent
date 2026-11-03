import json
import asyncio
import queue
import re
import threading
from difflib import unified_diff
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse

from app.core.config import Settings, get_settings
from app.api.schemas import (
    ApprovalRequest,
    AgentJobStatus,
    CommandBody,
    FileBody,
    FilesResponse,
    MessageRequest,
    MessageResponse,
    RenameSessionRequest,
    PlanSummary,
    SessionSummary,
    SessionsResponse,
    SessionStatus,
    ValidationBody,
    FileChange,
    OperationHistory,
    OperationRecord,
)
from app.services.agent import AgentCancelled, AgentTurn, InfraAgent
from app.services.approval import (
    clear_receipts,
    drop_receipt,
    matching_receipt,
    write_receipt,
)
from app.services.scanner import WorkspaceScanner
from app.services.sessions import (
    delete_session,
    finish_operation,
    list_operations,
    list_sessions,
    rename_session,
    require_session_id,
    session_dir,
    start_operation,
    touch_session,
)
from app.services.terraform import TerraformService
from app.services.pricing import AwsPricing
from app.services.workspace_lock import workspace_lease

router = APIRouter()
_JOB_STATUS_FILE = ".agent-job.json"
_JOB_LOCK = threading.Lock()
_JOB_EVENTS: dict[str, threading.Event] = {}


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
) -> AgentTurn:
    try:
        return agent.run(message, on_progress=publish, cancel_event=cancel_event)
    except TypeError as exc:
        if "unexpected keyword argument 'cancel_event'" not in str(exc):
            raise
        return agent.run(message, on_progress=publish)


@router.get("/sessions", response_model=SessionsResponse)
def get_sessions() -> SessionsResponse:
    return SessionsResponse(sessions=list_sessions())


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
    touch_session(session_id, body.message[:60])
    try:
        with workspace_lease(workspace):
            before = _workspace_snapshot(workspace)
            clear_receipts(workspace)
            turn = InfraAgent(workspace, settings).run(body.message)
            if turn.plan.ok and turn.scan.ok:
                write_receipt(workspace, "apply")
            response = _response(turn, _workspace_changes(workspace, before))
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
    touch_session(session_id, body.message[:60])
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
            turn = _run_agent(InfraAgent(workspace, settings), body.message, publish, cancel_event)
            if turn.plan.ok and turn.scan.ok:
                write_receipt(workspace, "apply")
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
    touch_session(session_id, body.message[:60])
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
            turn = _run_agent(InfraAgent(workspace, settings), body.message, publish, cancel_event)
            if turn.plan.ok and turn.scan.ok:
                write_receipt(workspace, "apply")
            response = _response(turn, _workspace_changes(workspace, before))
            _write_job_status(workspace, AgentJobStatus(status="completed", result=response))
            finish_operation(session_id, operation_id, "completed", response.reply)
            events.put(("result", response.model_dump()))
        except AgentCancelled as exc:
            error = str(exc)
            _write_job_status(workspace, AgentJobStatus(status="cancelled", error=error))
            finish_operation(session_id, operation_id, "cancelled", error)
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


@router.post("/sessions/{session_id}/plans/destroy", response_model=CommandBody)
def post_destroy_plan(session_id: str) -> CommandBody:
    workspace = _workspace_or_400(session_id, create=True)
    settings = get_settings()
    if not _has_aws(settings):
        drop_receipt(workspace, "destroy")
        raise HTTPException(status_code=503, detail="AWS credentials are not configured")
    operation_id = start_operation(session_id, "destroy-plan")
    try:
        with workspace_lease(workspace):
            result = TerraformService(workspace, settings).plan_destroy()
            if result.ok and _destroy_plan_has_changes(result.output()):
                write_receipt(workspace, "destroy")
            else:
                drop_receipt(workspace, "destroy")
    except Exception as exc:
        finish_operation(session_id, operation_id, "failed", _safe_detail(exc, settings))
        raise HTTPException(status_code=502, detail=_safe_detail(exc, settings)) from exc
    body = _command_body(result)
    finish_operation(session_id, operation_id, "completed" if result.ok else "failed", body.status)
    return body


@router.post("/sessions/{session_id}/apply", response_model=CommandBody)
def post_apply(session_id: str, body: ApprovalRequest) -> CommandBody:
    return _run_approved(session_id, body, "apply")


@router.post("/sessions/{session_id}/destroy", response_model=CommandBody)
def post_destroy(session_id: str, body: ApprovalRequest) -> CommandBody:
    return _run_approved(session_id, body, "destroy")


def _response(turn: AgentTurn, changes: list[FileChange] | None = None) -> MessageResponse:
    summary = _plan_summary(_redact(turn.plan.output()))
    if turn.plan.ok:
        estimate = AwsPricing(get_settings()).estimate(_redact(turn.plan.output()))
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
            output=_redact(turn.validation.output()),
            policy_warnings=turn.validation.policy_warnings,
        ),
        plan=ValidationBody(
            ok=turn.plan.ok,
            missing_binary=turn.plan.missing_binary,
            output=_redact(turn.plan.output()),
            policy_warnings=turn.plan.policy_warnings,
        ),
        scan=ValidationBody(
            ok=turn.scan.ok,
            missing_binary=turn.scan.missing_binary,
            output=_redact(turn.scan.output()),
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
    service = TerraformService(workspace, settings)
    operation_id = start_operation(session_id, kind)
    try:
        with workspace_lease(workspace):
            result = service.apply() if kind == "apply" else service.destroy()
            if result.ok:
                drop_receipt(workspace, "apply")
                drop_receipt(workspace, "destroy")
    except Exception as exc:
        finish_operation(session_id, operation_id, "failed", _safe_detail(exc, settings))
        raise HTTPException(status_code=502, detail=_safe_detail(exc, settings)) from exc
    body = _command_body(result)
    finish_operation(session_id, operation_id, "completed" if result.ok else "failed", body.status)
    return body


def _command_body(result) -> CommandBody:
    output = _redact(result.output())
    status = "completed"
    is_destroy = _is_destroy_command(result.command)
    if is_destroy and not _destroy_plan_has_changes(output):
        status = "no_managed_resources"
    elif not result.ok:
        status = "failed"
    summary = _plan_summary(output, destroy=is_destroy) if is_destroy else None
    return CommandBody(ok=result.ok, command=result.command, output=output, status=status, plan_summary=summary)


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
    if "aws_nat_gateway." in lower:
        warnings.append("NAT Gateway has hourly and per-GB processing charges.")
    if not match:
        return PlanSummary(resources=resources, cost_warnings=warnings)
    to_destroy = int(match.group(3))
    if destroy and destroy_resources:
        # Terraform can emit a stale/partial summary when refresh output is
        # interleaved with the destroy plan. The explicit resource actions are
        # authoritative for the review shown to the user.
        to_destroy = len(destroy_resources)
        resources = destroy_resources
    return PlanSummary(
        to_add=int(match.group(1)),
        to_change=int(match.group(2)),
        to_destroy=to_destroy,
        resources=resources,
        cost_warnings=warnings,
    )
