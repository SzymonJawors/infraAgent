import shutil
import subprocess
import sys
from pathlib import Path
import re

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
        output = "\n".join(part for part in (completed.stdout or "", stderr) if part)
        accepted = _accepted_findings(output)
        accepted_only = _only_accepted_findings(output)
        return CommandResult(
            ok=completed.returncode == 0 or accepted_only,
            command=command,
            stdout=(completed.stdout or "") + (
                "\nAccepted policy findings:\n"
                + "\n".join(
                    f"- {finding}: {_ACCEPTED_POLICY_FINDINGS[finding]}"
                    for finding in sorted(accepted)
                )
                if accepted
                else ""
            ),
            stderr=stderr,
            returncode=completed.returncode,
            missing_binary=missing,
            policy_warnings=sorted(accepted),
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


_ACCEPTED_POLICY_FINDINGS = {
    "CKV_AWS_382": (
        "A minimal EC2 workspace allows outbound traffic so the instance can reach "
        "SSM, package repositories, and AWS APIs. Restricting this requires VPC "
        "endpoints or a NAT design that was not requested."
    ),
    "CKV_AWS_18": (
        "S3 access logging is optional here; enabling it requires a separate "
        "logging bucket and adds storage and management cost."
    ),
    "CKV2_AWS_61": (
        "S3 lifecycle rules are optional here; no automatic deletion or retention "
        "period was requested, so object retention remains under user control."
    ),
    "CKV_AWS_144": (
        "S3 cross-region replication is optional here; it requires a second region "
        "and additional storage and transfer cost."
    ),
    "CKV2_AWS_62": (
        "S3 event notifications are optional here; no event consumer or notification "
        "workflow was requested."
    ),
}


def _accepted_findings(output: str) -> set[str]:
    return set(re.findall(r"CKV(?:2_)?_AWS_\d+", output)) & set(_ACCEPTED_POLICY_FINDINGS)


def _only_accepted_findings(output: str) -> bool:
    findings = set(re.findall(r"CKV(?:2_)?_AWS_\d+", output))
    return bool(findings) and findings <= set(_ACCEPTED_POLICY_FINDINGS)
