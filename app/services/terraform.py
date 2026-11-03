import os
import re
import subprocess
import threading
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Callable

from app.core.config import Settings
from app.services.workspace_lock import workspace_lease
from app.services.timing import Timing

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_CACHE_DIR = PROJECT_ROOT / ".terraform-plugin-cache"
CLI_CONFIG_PATH = PROJECT_ROOT / ".terraform-agent.tfrc"
_INIT_LOCK = threading.Lock()


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    command: str
    stdout: str
    stderr: str
    returncode: int | None
    missing_binary: bool = False
    policy_warnings: list[str] = field(default_factory=list)

    def full_output(self) -> str:
        chunks = [part.strip() for part in (self.stdout, self.stderr) if part.strip()]
        return "\n".join(chunks)

    def output(self, limit: int = 8000) -> str:
        text = self.full_output()
        if len(text) > limit:
            return text[:limit] + "\n...[truncated]"
        return text


def _skipped_plan(reason: str) -> CommandResult:
    return CommandResult(
        ok=False,
        command="terraform plan",
        stdout="",
        stderr=reason,
        returncode=None,
    )


class TerraformService:
    """Runs Terraform in one session directory.

    Chat review stops at plan. ``apply`` and ``destroy`` run only when an
    HTTP handler has already checked an approval receipt. Secrets are copied
    into the child environment and never written to ``.tf`` files.
    """

    def __init__(
        self,
        workspace: Path,
        settings: Settings,
        timeout: float = 120,
        timing: Timing | None = None,
        on_output: Callable[[str, str], None] | None = None,
    ) -> None:
        self.workspace = workspace
        self.settings = settings
        self.timeout = timeout
        self.timing = timing or Timing()
        self._initialized_signature: str | None = None
        self.on_output = on_output
        self._process: subprocess.Popen[str] | None = None
        self._process_lock = threading.Lock()

    def cancel(self) -> None:
        with self._process_lock:
            process = self._process
        if process is not None and process.poll() is None:
            process.terminate()

    def _provider_signature(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(self.workspace.glob("*.tf")):
            if path.name not in {"versions.tf", "provider.tf"}:
                continue
            digest.update(path.name.encode("utf-8"))
            digest.update(path.read_bytes())
        lockfile = self.workspace / ".terraform.lock.hcl"
        if lockfile.is_file():
            digest.update(lockfile.name.encode("utf-8"))
            digest.update(lockfile.read_bytes())
        return digest.hexdigest()

    def check(self, on_command: Callable[[str], None] | None = None) -> CommandResult:
        with workspace_lease(self.workspace):
            notify = on_command or (lambda _command: None)
            notify("terraform fmt -no-color")
            formatted = self._run(["terraform", "fmt", "-no-color"])
            if formatted.missing_binary or not formatted.ok:
                return formatted
            provider_signature = self._provider_signature()
            if self._initialized_signature != provider_signature or not (self.workspace / ".terraform").is_dir():
                notify("terraform init -backend=false -input=false -no-color")
                with _INIT_LOCK:
                    initialized = self._run(
                        ["terraform", "init", "-backend=false", "-input=false", "-no-color", "-upgrade=false"],
                        timeout=max(self.timeout, 900),
                    )
                if initialized.missing_binary or not initialized.ok:
                    return initialized
                self._initialized_signature = self._provider_signature()
            notify("terraform validate -no-color")
            return self._run(["terraform", "validate", "-no-color"])

    def review(self, on_command: Callable[[str], None] | None = None) -> tuple[CommandResult, CommandResult]:
        """Validate the session, then plan when AWS credentials are present.

        ``apply`` and ``destroy`` are not part of this path.
        """
        with workspace_lease(self.workspace):
            validation = self.check(on_command)
            if not validation.ok:
                return validation, _skipped_plan("Skipped because validation did not pass.")
            if not self._has_aws():
                return validation, _skipped_plan(
                    "AWS credentials are not configured. Plan was not sent to AWS."
                )
            if on_command:
                on_command("terraform plan -input=false -no-color -lock=false")
            planned = self._run(
                ["terraform", "plan", "-input=false", "-no-color", "-lock=false"],
                timeout=max(self.timeout, 180),
            )
            return validation, planned

    def plan_destroy(self) -> CommandResult:
        """Show what destroy would remove. This does not delete resources."""
        with workspace_lease(self.workspace):
            if not self._has_aws():
                return _skipped_plan(
                    "AWS credentials are not configured. Plan was not sent to AWS."
                )
            return self._run(
                ["terraform", "plan", "-destroy", "-input=false", "-no-color", "-lock=false"],
                timeout=max(self.timeout, 180),
            )

    def apply(self) -> CommandResult:
        with workspace_lease(self.workspace):
            return self._run(
                ["terraform", "apply", "-input=false", "-auto-approve", "-no-color", "-lock=false"],
                timeout=max(self.timeout, 900),
            )

    def destroy(self) -> CommandResult:
        with workspace_lease(self.workspace):
            protection_error = self._disable_termination_protection()
            if protection_error is not None:
                return protection_error
            return self._run(
                ["terraform", "destroy", "-input=false", "-auto-approve", "-no-color", "-lock=false"],
                timeout=max(self.timeout, 900),
            )

    def _disable_termination_protection(self) -> CommandResult | None:
        """Allow an approved destroy to remove protected EC2 instances."""
        try:
            listed = subprocess.run(
                ["terraform", "state", "list"],
                cwd=self.workspace,
                capture_output=True,
                text=True,
                timeout=60,
                env=self._env(),
                check=False,
            )
        except FileNotFoundError:
            return CommandResult(
                ok=False,
                command="terraform state list",
                stdout="",
                stderr="Terraform CLI is required before destroying protected EC2 instances.",
                returncode=None,
                missing_binary=True,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(
                ok=False,
                command="terraform state list",
                stdout="",
                stderr="Timed out while inspecting Terraform state before destroy.",
                returncode=None,
            )
        if listed.returncode != 0:
            return CommandResult(
                ok=False,
                command="terraform state list",
                stdout=listed.stdout,
                stderr=listed.stderr or "Could not inspect Terraform state before destroy.",
                returncode=listed.returncode,
            )

        addresses = [
            line.strip()
            for line in listed.stdout.splitlines()
            if line.strip().startswith("aws_instance.")
        ]
        for address in addresses:
            try:
                state = subprocess.run(
                    ["terraform", "state", "show", "-no-color", address],
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    timeout=60,
                    env=self._env(),
                    check=False,
                )
            except FileNotFoundError:
                return CommandResult(
                    ok=False,
                    command="terraform state show",
                    stdout="",
                    stderr="Terraform CLI is required before destroying protected EC2 instances.",
                    returncode=None,
                    missing_binary=True,
                )
            except subprocess.TimeoutExpired:
                return CommandResult(
                    ok=False,
                    command="terraform state show",
                    stdout="",
                    stderr=f"Timed out while inspecting {address} before destroy.",
                    returncode=None,
                )
            if state.returncode != 0:
                return CommandResult(
                    ok=False,
                    command=f"terraform state show {address}",
                    stdout=state.stdout,
                    stderr=state.stderr or f"Could not inspect {address} before destroy.",
                    returncode=state.returncode,
                )
            if 'disable_api_termination = true' not in state.stdout:
                continue
            instance_id = re.search(r'^\s*id\s*=\s*"(i-[^"]+)"\s*$', state.stdout, re.MULTILINE)
            if not instance_id:
                return CommandResult(
                    ok=False,
                    command="aws ec2 modify-instance-attribute",
                    stdout="",
                    stderr=f"Could not determine the instance id for protected {address}.",
                    returncode=None,
                )
            try:
                modified = subprocess.run(
                    [
                        "aws",
                        "ec2",
                        "modify-instance-attribute",
                        "--instance-id",
                        instance_id.group(1),
                        "--no-disable-api-termination",
                        "--region",
                        self.settings.aws_region,
                    ],
                    cwd=self.workspace,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=self._env(),
                    check=False,
                )
            except FileNotFoundError:
                return CommandResult(
                    ok=False,
                    command="aws ec2 modify-instance-attribute",
                    stdout="",
                    stderr="AWS CLI is required to disable EC2 termination protection before destroy.",
                    returncode=None,
                    missing_binary=True,
                )
            except subprocess.TimeoutExpired:
                return CommandResult(
                    ok=False,
                    command="aws ec2 modify-instance-attribute",
                    stdout="",
                    stderr="Timed out while disabling EC2 termination protection.",
                    returncode=None,
                )
            if modified.returncode != 0:
                return CommandResult(
                    ok=False,
                    command="aws ec2 modify-instance-attribute",
                    stdout=modified.stdout,
                    stderr=modified.stderr,
                    returncode=modified.returncode,
                )
        return None

    def _has_aws(self) -> bool:
        return bool(self.settings.aws_access_key_id and self.settings.aws_secret_access_key)

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        # Do not combine Terraform's legacy plugin cache with the filesystem
        # mirror below; doing so can make init copy a provider directory onto
        # itself.
        env.pop("TF_PLUGIN_CACHE_DIR", None)
        PLUGIN_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cli_config = (
            "provider_installation {\n"
            "  filesystem_mirror {\n"
            f'    path    = "{PLUGIN_CACHE_DIR.as_posix()}"\n'
            '    include = ["registry.terraform.io/hashicorp/*"]\n'
            "  }\n"
            "  direct {}\n"
            "}\n"
        )
        if not CLI_CONFIG_PATH.is_file() or CLI_CONFIG_PATH.read_text(encoding="utf-8") != cli_config:
            CLI_CONFIG_PATH.write_text(cli_config, encoding="utf-8")
        env["TF_CLI_CONFIG_FILE"] = str(CLI_CONFIG_PATH)
        if self.settings.aws_access_key_id:
            env["AWS_ACCESS_KEY_ID"] = self.settings.aws_access_key_id
        if self.settings.aws_secret_access_key:
            env["AWS_SECRET_ACCESS_KEY"] = self.settings.aws_secret_access_key
        if self.settings.aws_session_token:
            env["AWS_SESSION_TOKEN"] = self.settings.aws_session_token
        if self.settings.aws_region:
            env["AWS_REGION"] = self.settings.aws_region
            env["AWS_DEFAULT_REGION"] = self.settings.aws_region
        return env

    def _run(self, args: list[str], timeout: float | None = None) -> CommandResult:
        command = " ".join(args)
        limit = self.timeout if timeout is None else timeout
        metric = "_".join(part for part in args if part.startswith("-") is False)
        try:
            with self.timing.measure(metric):
                process = subprocess.Popen(
                    args,
                    cwd=self.workspace,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    env=self._env(),
                )
                with self._process_lock:
                    self._process = process
                lines: list[str] = []
                try:
                    assert process.stdout is not None
                    for line in process.stdout:
                        text_line = line.rstrip("\r\n")
                        lines.append(text_line)
                        if self.on_output:
                            self.on_output(text_line, command)
                    process.wait(timeout=limit)
                finally:
                    with self._process_lock:
                        self._process = None
                completed_returncode = process.returncode
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
            with self._process_lock:
                process = self._process
            if process is not None and process.poll() is None:
                process.terminate()
            return CommandResult(
                ok=False,
                command=command,
                stdout="\n".join(lines),
                stderr=f"timed out after {limit}s",
                returncode=None,
            )
        stdout = "\n".join(lines)
        return CommandResult(
            ok=completed_returncode == 0,
            command=command,
            stdout=stdout,
            stderr="",
            returncode=completed_returncode,
        )
