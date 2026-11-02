from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException

from app.core.config import Settings, get_settings
from app.services.agent import AgentTurn, InfraAgent
from app.services.approval import (
    clear_receipts,
    drop_receipt,
    matching_receipt,
    write_receipt,
)
from app.services.sessions import require_session_id, session_dir
from app.services.terraform import TerraformService

router = APIRouter()


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)


class ValidationBody(BaseModel):
    ok: bool
    missing_binary: bool
    output: str


class MessageResponse(BaseModel):
    reply: str
    files: list[str]
    validation: ValidationBody
    plan: ValidationBody
    scan: ValidationBody
    registry_docs: bool


class ApprovalRequest(BaseModel):
    approve: bool


class CommandBody(BaseModel):
    ok: bool
    command: str
    output: str


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
    clear_receipts(workspace)

    try:
        turn = InfraAgent(workspace, settings).run(body.message)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_detail(exc, settings)) from exc
    if turn.plan.ok and turn.scan.ok:
        write_receipt(workspace, "apply")
    return _response(turn)


@router.post("/sessions/{session_id}/plans/destroy", response_model=CommandBody)
def post_destroy_plan(session_id: str) -> CommandBody:
    workspace = _workspace_or_400(session_id)
    settings = get_settings()
    if not _has_aws(settings):
        drop_receipt(workspace, "destroy")
        raise HTTPException(status_code=503, detail="AWS credentials are not configured")
    result = TerraformService(workspace, settings).plan_destroy()
    if result.ok:
        write_receipt(workspace, "destroy")
    else:
        drop_receipt(workspace, "destroy")
    return _command_body(result)


@router.post("/sessions/{session_id}/apply", response_model=CommandBody)
def post_apply(session_id: str, body: ApprovalRequest) -> CommandBody:
    return _run_approved(session_id, body, "apply")


@router.post("/sessions/{session_id}/destroy", response_model=CommandBody)
def post_destroy(session_id: str, body: ApprovalRequest) -> CommandBody:
    return _run_approved(session_id, body, "destroy")


def _response(turn: AgentTurn) -> MessageResponse:
    return MessageResponse(
        reply=turn.reply,
        files=turn.files,
        validation=ValidationBody(
            ok=turn.validation.ok,
            missing_binary=turn.validation.missing_binary,
            output=_redact(turn.validation.output()),
        ),
        plan=ValidationBody(
            ok=turn.plan.ok,
            missing_binary=turn.plan.missing_binary,
            output=_redact(turn.plan.output()),
        ),
        scan=ValidationBody(
            ok=turn.scan.ok,
            missing_binary=turn.scan.missing_binary,
            output=_redact(turn.scan.output()),
        ),
        registry_docs=turn.registry_docs,
    )


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


def _workspace_or_400(session_id: str):
    try:
        require_session_id(session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid session id") from exc
    return session_dir(session_id)


def _has_aws(settings: Settings) -> bool:
    return bool(settings.aws_access_key_id and settings.aws_secret_access_key)


def _run_approved(session_id: str, body: ApprovalRequest, kind: str) -> CommandBody:
    if body.approve is not True:
        raise HTTPException(status_code=400, detail="Approval is required")
    workspace = _workspace_or_400(session_id)
    settings = get_settings()
    if not _has_aws(settings):
        raise HTTPException(status_code=503, detail="AWS credentials are not configured")
    if not matching_receipt(workspace, kind):
        raise HTTPException(
            status_code=409,
            detail=f"No matching {kind} plan for the current files",
        )
    service = TerraformService(workspace, settings)
    result = service.apply() if kind == "apply" else service.destroy()
    if result.ok:
        drop_receipt(workspace, "apply")
        drop_receipt(workspace, "destroy")
    return _command_body(result)


def _command_body(result) -> CommandBody:
    return CommandBody(ok=result.ok, command=result.command, output=_redact(result.output()))

