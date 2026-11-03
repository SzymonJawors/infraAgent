import os
import threading
from pathlib import Path


class WorkspaceBusyError(RuntimeError):
    """Raised when another process currently owns a workspace."""


class WorkspaceLease:
    """A re-entrant process-local and cross-process workspace lock."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.lock_path = self.workspace / ".infraagent" / "workspace.lock"
        self._file = None
        self._thread_lock = _lock_for(self.workspace)
        self._owner_key = (threading.get_ident(), self.workspace)

    def acquire(self) -> "WorkspaceLease":
        if not self._thread_lock.acquire(blocking=False):
            raise WorkspaceBusyError("workspace is busy")
        depth = _depths.get(self._owner_key, 0)
        _depths[self._owner_key] = depth + 1
        if depth:
            return self
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._file = self.lock_path.open("a+b")
            _lock_file(self._file)
        except (OSError, BlockingIOError):
            if self._file is not None:
                self._file.close()
                self._file = None
            _depths.pop(self._owner_key, None)
            self._thread_lock.release()
            raise WorkspaceBusyError("workspace is busy")
        return self

    def release(self) -> None:
        depth = _depths.get(self._owner_key, 0)
        if depth <= 1:
            _depths.pop(self._owner_key, None)
        else:
            _depths[self._owner_key] = depth - 1
        if self._file is not None:
            try:
                _unlock_file(self._file)
            finally:
                self._file.close()
                self._file = None
        self._thread_lock.release()

    def __enter__(self) -> "WorkspaceLease":
        return self.acquire()

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.release()


def workspace_lease(workspace: Path) -> WorkspaceLease:
    return WorkspaceLease(workspace)


def workspace_is_busy(workspace: Path) -> bool:
    path = workspace.resolve() / ".infraagent" / "workspace.lock"
    if not path.is_file():
        return False
    try:
        with path.open("a+b") as handle:
            _lock_file(handle, blocking=False)
            _unlock_file(handle)
            return False
    except (OSError, BlockingIOError):
        return True


_LOCKS: dict[Path, threading.RLock] = {}
_depths: dict[tuple[int, Path], int] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(workspace: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(workspace, threading.RLock())


def _lock_file(handle, blocking: bool = True) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        msvcrt.locking(handle.fileno(), mode, 1)
        return
    import fcntl

    flags = fcntl.LOCK_EX
    if not blocking:
        flags |= fcntl.LOCK_NB
    fcntl.flock(handle.fileno(), flags)


def _unlock_file(handle) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
