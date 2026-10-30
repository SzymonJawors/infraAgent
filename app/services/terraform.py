import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    command: str
    stdout: str
    stderr: str
    returncode: int | None
    missing_binary: bool = False

    def output(self) -> str:
        chunks = [part.strip() for part in (self.stdout, self.stderr) if part.strip()]
        text = "\n".join(chunks)
        if len(text) > 8000:
            return text[:8000] + "\n...[truncated]"
        return text


class TerraformService:
    """Runs fmt, init, and validate against one session directory.

    ``apply`` and ``destroy`` are intentionally absent. Secrets are copied
    into the child environment and never written to ``.tf`` files.
    """

    def __init__(self, workspace: Path, settings: Settings, timeout: float = 120) -> None:
        self.workspace = workspace
        self.settings = settings
        self.timeout = timeout

    def check(self) -> CommandResult:
        formatted = self._run(["terraform", "fmt", "-no-color"])
        if formatted.missing_binary or not formatted.ok:
            return formatted
        initialized = self._run(
            ["terraform", "init", "-backend=false", "-input=false", "-no-color"]
        )
        if initialized.missing_binary or not initialized.ok:
            return initialized
        return self._run(["terraform", "validate", "-no-color"])

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.settings.aws_access_key_id:
            env["AWS_ACCESS_KEY_ID"] = self.settings.aws_access_key_id
        if self.settings.aws_secret_access_key:
            env["AWS_SECRET_ACCESS_KEY"] = self.settings.aws_secret_access_key
        if self.settings.aws_region:
            env["AWS_REGION"] = self.settings.aws_region
            env["AWS_DEFAULT_REGION"] = self.settings.aws_region
        return env

    def _run(self, args: list[str]) -> CommandResult:
        command = " ".join(args)
        try:
            completed = subprocess.run(
                args,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(
                ok=False,
                command=command,
                stdout="",
                stderr="terraform CLI is not on PATH",
                returncode=None,
                missing_binary=True,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout or ""
            stderr = exc.stderr or ""
            if isinstance(stdout, bytes):
                stdout = stdout.decode("utf-8", errors="replace")
            if isinstance(stderr, bytes):
                stderr = stderr.decode("utf-8", errors="replace")
            return CommandResult(
                ok=False,
                command=command,
                stdout=stdout,
                stderr=stderr or f"timed out after {self.timeout}s",
                returncode=None,
            )
        return CommandResult(
            ok=completed.returncode == 0,
            command=command,
            stdout=completed.stdout,
            stderr=completed.stderr,
            returncode=completed.returncode,
        )
