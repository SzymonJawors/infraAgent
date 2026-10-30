import re
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / "workspace"
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def require_session_id(session_id: str) -> str:
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("invalid session id")
    return session_id


def session_dir(session_id: str, root: Path = WORKSPACE_ROOT) -> Path:
    """Return the session directory, creating it when the id is a safe name."""
    require_session_id(session_id)
    root_resolved = root.resolve()
    path = (root_resolved / session_id).resolve()
    if path.parent != root_resolved:
        raise ValueError("invalid session id")
    path.mkdir(parents=True, exist_ok=True)
    return path
