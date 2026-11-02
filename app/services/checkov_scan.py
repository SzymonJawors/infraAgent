import shutil
import subprocess
import sys
from pathlib import Path

from app.services.terraform import CommandResult


class CheckovService:
    """Runs Checkov against one session directory.

    The scan reads ``.tf`` files only. It does not call AWS and it does not
    apply or destroy anything.
    """

    def __init__(self, workspace: Path, timeout: float = 180) -> None:
        self.workspace = workspace
        self.timeout = timeout

    def scan(self) -> CommandResult:
        if not any(path.is_file() for path in self.workspace.glob("*.tf")):
            return _skipped("No Terraform files were written.")
        executable = _command()
        if executable is None:
            return CommandResult(
                ok=False,
                command="checkov",
                stdout="",
                stderr="checkov is not installed",
                returncode=None,
                missing_binary=True,
            )
        command = "checkov"
        args = [
            *executable,
            "-d",
            str(self.workspace),
            "--framework",
            "terraform",
            "--quiet",
            "--compact",
            "--download-external-modules",
            "false",
            "--skip-path",
            ".terraform",
        ]
        try:
            completed = subprocess.run(
                args,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _text(exc.stdout)
            stderr = _text(exc.stderr) or f"timed out after {self.timeout}s"
            return CommandResult(
                ok=False,
                command=command,
                stdout=stdout,
                stderr=stderr,
                returncode=None,
            )
        stderr = completed.stderr or ""
        missing = "No module named checkov" in stderr or "No module named checkov" in (
            completed.stdout or ""
        )
        return CommandResult(
            ok=completed.returncode == 0,
            command=command,
            stdout=completed.stdout or "",
            stderr=stderr,
            returncode=completed.returncode,
            missing_binary=missing,
        )


def _command() -> list[str] | None:
    script = Path(sys.executable).parent / "checkov"
    if script.is_file():
        return [sys.executable, str(script)]
    found = shutil.which("checkov")
    if found:
        return [found]
    return None


def _skipped(reason: str) -> CommandResult:
    return CommandResult(
        ok=False,
        command="checkov",
        stdout="",
        stderr=reason,
        returncode=None,
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value
