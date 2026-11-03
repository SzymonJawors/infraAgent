from dataclasses import dataclass
from typing import Callable

from app.services.approval import files_digest
from app.services.checkov_scan import CheckovService
from app.services.terraform import CommandResult, TerraformService
from app.services.timing import Timing


@dataclass
class ReviewState:
    results: tuple[CommandResult, CommandResult, CommandResult] | None = None
    digest: str | None = None


class ReviewAgent:
    """Runs deterministic Terraform validation, security scanning, and planning."""

    def __init__(
        self,
        workspace,
        terraform: TerraformService,
        checkov: CheckovService,
        progress: Callable[[str, str, str | None], None],
        command_message: Callable[[str], str],
        max_attempts: int = 5,
        timing: Timing | None = None,
        skip_checkov: bool = False,
        preflight: Callable[[], str | None] | None = None,
    ) -> None:
        self.workspace = workspace
        self.terraform = terraform
        self.checkov = checkov
        self.progress = progress
        self.command_message = command_message
        self.max_attempts = max_attempts
        self.timing = timing or Timing()
        self.skip_checkov = skip_checkov
        self.preflight = preflight
        self.attempts = 0
        self.exhausted = False
        self.state = ReviewState()
        self.cache: dict[
            str,
            tuple[tuple[CommandResult, CommandResult, CommandResult], str],
        ] = {}

    def check(self, ensure_active: Callable[[], None]) -> str:
        ensure_active()
        current_digest = files_digest(self.workspace)
        cached = self.cache.get(current_digest)
        if cached is not None:
            self.state.results, message = cached
            self.state.digest = current_digest
            return message

        preflight_results = self._preflight_results()
        if preflight_results:
            validation, plan, scan, preflight_error = preflight_results
            results = (validation, plan, scan)
            message = f"configuration error, fix the infrastructure and check again\n{preflight_error}"
            self.cache[current_digest] = (results, message)
            self.state = ReviewState(results=results, digest=current_digest)
            return message

        self.attempts += 1
        if self.attempts > self.max_attempts:
            self.exhausted = True
            return "Check attempt limit reached. Stop and summarize the remaining error."

        with self.timing.measure("terraform_review"):
            validation, plan = self.terraform.review(
                on_command=lambda command: self.progress(
                    "executing",
                    self.command_message(command),
                    command,
                )
            )
        scan = self._scan(validation)
        results = (validation, plan, scan)
        body = (
            f"validation {'ok' if validation.ok else 'failed'}\n{model_output(validation.output())}\n"
            f"scan {'ok' if scan.ok else 'failed'}\n{model_output(scan.output())}\n"
            f"plan {'ok' if plan.ok else 'failed'}\n{model_output(plan.output())}"
        ).strip()
        message = (
            f"ok\n{body}"
            if validation.ok and scan.ok and (plan.ok or credentials_missing(plan))
            else f"configuration error, fix the HCL and check again\n{body}"
        )
        stop = non_retryable(*results)
        if stop:
            message = f"Do not rewrite the files. {stop}\n{body}"
        review_digest = files_digest(self.workspace)
        self.cache[current_digest] = (results, message)
        self.cache[review_digest] = (results, message)
        self.state = ReviewState(results=results, digest=review_digest)
        return message

    def current(self) -> tuple[CommandResult, CommandResult, CommandResult] | None:
        return self.state.results

    def final(self, ensure_active: Callable[[], None]):
        ensure_active()
        if self.state.results is not None and self.state.digest == files_digest(self.workspace):
            return self.state.results
        preflight_results = self._preflight_results()
        if preflight_results:
            validation, plan, scan, _ = preflight_results
            self.state = ReviewState(
                results=(validation, plan, scan),
                digest=files_digest(self.workspace),
            )
            return self.state.results
        with self.timing.measure("terraform_review"):
            validation, plan = self.terraform.review(
                on_command=lambda command: self.progress(
                    "executing", self.command_message(command), command
                )
            )
        scan = self._scan(validation)
        self.state = ReviewState(
            results=(validation, plan, scan),
            digest=files_digest(self.workspace),
        )
        return self.state.results

    def _preflight_results(self):
        preflight_error = self.preflight() if self.preflight else None
        if not preflight_error:
            return None
        validation = CommandResult(
            ok=False,
            command="infrastructure preflight",
            stdout="",
            stderr=preflight_error,
            returncode=1,
        )
        plan = CommandResult(
            ok=False,
            command="terraform plan",
            stdout="",
            stderr="Skipped until the infrastructure preflight passes.",
            returncode=None,
        )
        scan = CommandResult(
            ok=False,
            command="infrastructure preflight",
            stdout="",
            stderr=preflight_error,
            returncode=1,
        )
        return validation, plan, scan, preflight_error

    def _scan(self, validation: CommandResult) -> CommandResult:
        if self.skip_checkov:
            return skipped_scan("Skipped by user for this request.")
        self.progress(
            "scanning",
            "Running security scan...",
            "checkov -d <workspace> --framework terraform",
        )
        with self.timing.measure("checkov"):
            return self.checkov.scan() if validation.ok else skipped_scan(
                "Skipped because validation did not pass."
            )


def skipped_scan(reason: str) -> CommandResult:
    return CommandResult(
        ok=True,
        command="checkov",
        stdout="",
        stderr=reason,
        returncode=None,
        policy_warnings=["checkov_skipped"],
    )


def credentials_missing(plan: CommandResult) -> bool:
    text = plan.output().lower()
    return "aws credentials are not configured" in text or "no valid credential sources" in text


def non_retryable(
    validation: CommandResult,
    plan: CommandResult,
    scan: CommandResult,
) -> str | None:
    text = f"{validation.output()}\n{plan.output()}\n{scan.output()}".lower()
    if validation.missing_binary:
        return "terraform CLI is missing"
    if scan.missing_binary:
        return "checkov is missing"
    if "timed out" in text:
        return "provider download timed out"
    if credentials_missing(plan) and (scan.ok or not validation.ok):
        return "AWS credentials are missing"
    return None


def model_output(text: str, limit: int = 3000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated for model context]"
