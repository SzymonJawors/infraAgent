from pydantic import BaseModel, Field
from fastapi import APIRouter, HTTPException

from app.core.config import Settings, get_settings
from app.services.agent import AgentTurn, InfraAgent
from app.services.sessions import require_session_id, session_dir

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
    registry_docs: bool


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

    try:
        turn = InfraAgent(workspace, settings).run(body.message)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=_safe_detail(exc, settings)) from exc
    return _response(turn)


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
