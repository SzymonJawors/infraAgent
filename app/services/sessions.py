import re
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

WORKSPACE_ROOT = Path(__file__).resolve().parents[2] / "workspace"
SESSION_DB = WORKSPACE_ROOT / ".sessions.sqlite3"
_SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def require_session_id(session_id: str) -> str:
    if _SESSION_ID.fullmatch(session_id) is None:
        raise ValueError("invalid session id")
    return session_id


def session_dir(
    session_id: str,
    root: Path = WORKSPACE_ROOT,
    *,
    create: bool = True,
) -> Path:
    """Return a safe session directory, optionally creating it."""
    require_session_id(session_id)
    root_resolved = root.resolve()
    path = (root_resolved / session_id).resolve()
    if path.parent != root_resolved:
        raise ValueError("invalid session id")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def _connect() -> sqlite3.Connection:
    WORKSPACE_ROOT.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(SESSION_DB, timeout=5)
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            summary TEXT NOT NULL DEFAULT ''
            ,command TEXT NOT NULL DEFAULT ''
            ,output TEXT NOT NULL DEFAULT ''
            ,ok INTEGER
        )
        """
    )
    for column, definition in (
        ("command", "TEXT NOT NULL DEFAULT ''"),
        ("output", "TEXT NOT NULL DEFAULT ''"),
        ("ok", "INTEGER"),
    ):
        try:
            connection.execute(f"ALTER TABLE operations ADD COLUMN {column} {definition}")
        except sqlite3.OperationalError:
            pass
    return connection


def start_operation(session_id: str, kind: str) -> int:
    require_session_id(session_id)
    started = datetime.now(UTC).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            "INSERT INTO operations (session_id, kind, status, started_at) VALUES (?, ?, ?, ?)",
            (session_id, kind, "running", started),
        )
        return int(cursor.lastrowid)


def finish_operation(
    session_id: str,
    operation_id: int,
    status: str,
    summary: str = "",
    *,
    command: str = "",
    output: str = "",
    ok: bool | None = None,
) -> None:
    require_session_id(session_id)
    with _connect() as connection:
        connection.execute(
            """
            UPDATE operations
            SET status = ?, finished_at = ?, summary = ?, command = ?, output = ?, ok = ?
            WHERE id = ? AND session_id = ?
            """,
            (
                status,
                datetime.now(UTC).isoformat(),
                summary[:500],
                command,
                output,
                None if ok is None else int(ok),
                operation_id,
                session_id,
            ),
        )


def update_operation_output(
    session_id: str,
    operation_id: int,
    output: str,
    *,
    command: str = "",
) -> None:
    require_session_id(session_id)
    with _connect() as connection:
        connection.execute(
            """
            UPDATE operations
            SET output = ?, command = COALESCE(NULLIF(?, ''), command)
            WHERE id = ? AND session_id = ?
            """,
            (output, command, operation_id, session_id),
        )


def list_operations(session_id: str, limit: int = 30) -> list[dict[str, object]]:
    require_session_id(session_id)
    with _connect() as connection:
        rows = connection.execute(
            """
            SELECT id, kind, status, started_at, finished_at, summary, command, output, ok
            FROM operations
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [
        {
            "id": row[0],
            "kind": row[1],
            "status": row[2],
            "started_at": row[3],
            "finished_at": row[4],
            "summary": row[5],
            "command": row[6],
            "output": row[7],
            "ok": None if row[8] is None else bool(row[8]),
        }
        for row in rows
    ]


def get_operation(session_id: str, operation_id: int) -> dict[str, object] | None:
    require_session_id(session_id)
    with _connect() as connection:
        row = connection.execute(
            """
            SELECT id, kind, status, started_at, finished_at, summary, command, output, ok
            FROM operations
            WHERE id = ? AND session_id = ?
            """,
            (operation_id, session_id),
        ).fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "kind": row[1],
        "status": row[2],
        "started_at": row[3],
        "finished_at": row[4],
        "summary": row[5],
        "command": row[6],
        "output": row[7],
        "ok": None if row[8] is None else bool(row[8]),
    }


def touch_session(session_id: str, title: str | None = None) -> None:
    require_session_id(session_id)
    now = datetime.now(UTC).isoformat()
    with _connect() as connection:
        if title is None:
            cursor = connection.execute(
                """
                UPDATE sessions
                SET updated_at = ?
                WHERE id = ?
                """,
                (now, session_id),
            )
            if cursor.rowcount == 0:
                title = "Untitled environment"
        if title is not None:
            connection.execute(
                """
                INSERT INTO sessions (id, title, created_at, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = CASE WHEN excluded.title != '' THEN excluded.title ELSE sessions.title END,
                    updated_at = excluded.updated_at
                """,
                (session_id, title, now, now),
            )


def list_sessions() -> list[dict[str, str]]:
    known = {item["id"] for item in _list_stored_sessions()}
    for path in WORKSPACE_ROOT.iterdir() if WORKSPACE_ROOT.exists() else ():
        if path.is_dir() and _SESSION_ID.fullmatch(path.name) and path.name not in known:
            touch_session(path.name, path.name)
    with _connect() as connection:
        rows = connection.execute(
            "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC"
        ).fetchall()
    return [
        {"id": row[0], "title": row[1], "created_at": row[2], "updated_at": row[3]}
        for row in rows
    ]


def delete_session(session_id: str) -> None:
    require_session_id(session_id)
    path = (WORKSPACE_ROOT / session_id).resolve()
    if path.parent != WORKSPACE_ROOT.resolve():
        raise ValueError("invalid session id")
    if path.exists():
        if not path.is_dir():
            raise OSError("session path is not a directory")
        shutil.rmtree(path)
    with _connect() as connection:
        connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        connection.execute("DELETE FROM operations WHERE session_id = ?", (session_id,))


def rename_session(session_id: str, title: str) -> dict[str, str]:
    require_session_id(session_id)
    clean_title = " ".join(title.split())
    if not clean_title:
        raise ValueError("session title cannot be empty")
    if len(clean_title) > 80:
        raise ValueError("session title is too long")
    now = datetime.now(UTC).isoformat()
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?",
            (clean_title, now, session_id),
        )
        if cursor.rowcount == 0:
            raise KeyError("session not found")
        row = connection.execute(
            "SELECT id, title, created_at, updated_at FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    return {"id": row[0], "title": row[1], "created_at": row[2], "updated_at": row[3]}


def _list_stored_sessions() -> list[dict[str, str]]:
    with _connect() as connection:
        rows = connection.execute("SELECT id FROM sessions").fetchall()
    return [{"id": row[0]} for row in rows]
