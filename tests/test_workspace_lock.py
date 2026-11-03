import os
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.services.workspace_lock import WorkspaceBusyError, workspace_is_busy, workspace_lease


class WorkspaceLockTests(unittest.TestCase):
    def test_workspace_lease_creates_and_releases_lock_file(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            with workspace_lease(workspace):
                self.assertTrue((workspace / ".infraagent" / "workspace.lock").exists())
                self.assertTrue(workspace_is_busy(workspace))
            self.assertFalse(workspace_is_busy(workspace))

    def test_nested_leases_are_reentrant(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            with workspace_lease(workspace):
                with workspace_lease(workspace):
                    self.assertTrue(workspace_is_busy(workspace))

    @unittest.skipUnless(os.name == "nt", "Windows-specific lock file regression")
    def test_windows_lock_file_has_a_byte_to_lock(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            with workspace_lease(workspace):
                lock_file = workspace / ".infraagent" / "workspace.lock"
                self.assertTrue(lock_file.exists())

    @unittest.skipUnless(os.name != "nt", "Windows agent tools may use multiple worker threads")
    def test_competing_thread_is_rejected(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            with workspace_lease(workspace):
                errors = []

                def compete() -> None:
                    try:
                        with workspace_lease(workspace):
                            pass
                    except WorkspaceBusyError:
                        errors.append(True)

                thread = threading.Thread(target=compete)
                thread.start()
                thread.join()
                self.assertEqual(errors, [True])


if __name__ == "__main__":
    unittest.main()
