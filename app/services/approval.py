import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

_KINDS = ("apply", "destroy")
APPROVAL_TTL_SECONDS = 30 * 60


def files_digest(workspace: Path) -> str:
    """Hash the session ``*.tf`` files. Order is the file name."""
    hasher = hashlib.sha256()
    for path in sorted(workspace.glob("*.tf")):
        if not path.is_file():
            continue
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def receipt_path(workspace: Path, kind: str) -> Path:
    if kind not in _KINDS:
        raise ValueError("invalid receipt kind")
    return workspace / ".infraagent" / f"{kind}.json"


def clear_receipts(workspace: Path) -> None:
    for kind in _KINDS:
        drop_receipt(workspace, kind)


def write_receipt(
    workspace: Path,
    kind: str,
    *,
    plan_output: str = "",
    session_id: str | None = None,
) -> None:
    path = receipt_path(workspace, kind)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": kind,
        "digest": files_digest(workspace),
        "created_at": datetime.now(UTC).isoformat(),
        "session_id": session_id or workspace.name,
        "plan_output": plan_output,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def drop_receipt(workspace: Path, kind: str) -> None:
    receipt_path(workspace, kind).unlink(missing_ok=True)


def matching_receipt(workspace: Path, kind: str) -> bool:
    return read_receipt(workspace, kind) is not None


def read_receipt(workspace: Path, kind: str) -> dict | None:
    path = receipt_path(workspace, kind)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("kind") != kind or data.get("digest") != files_digest(workspace):
        return None
    try:
        created_at = datetime.fromisoformat(data["created_at"])
    except (KeyError, TypeError, ValueError):
        drop_receipt(workspace, kind)
        return None
    age = (datetime.now(UTC) - created_at).total_seconds()
    if age < 0 or age > APPROVAL_TTL_SECONDS:
        drop_receipt(workspace, kind)
        return None
    return data
