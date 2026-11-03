import asyncio
import json
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError

from app.core.config import Settings
from app.services.checkov_scan import CheckovService
from app.services.hcl_guard import rejected_hcl
from app.services.scanner import WorkspaceScanner
from app.services.approval import files_digest
from app.services.terraform import CommandResult, TerraformService
from app.services.workspace_lock import workspace_lease
from app.services.builder import BuilderAgent
from app.services.planner import PlannerAgent
from app.services.reviewer import ReviewAgent, credentials_missing, non_retryable
from app.services.timing import Timing

_SYSTEM_PROMPT = """# Role

You are InfraAgent, a tool-using AWS Terraform agent. Translate the user's infrastructure request
into Terraform in the current session workspace, validate the result, and explain what actually
changed. Do not only describe code that could be written.

# Capabilities

- **write_tf(filename, content)** — create or replace one plain `*.tf` file in the session.
- **read_workspace()** — inspect all existing Terraform files before a follow-up change.
- **check_infrastructure()** — format, initialize, validate, scan with Checkov, and plan when AWS
  credentials exist. It never applies or destroys infrastructure.
- **Terraform Registry tools** — use them for resource documentation when available; continue from
  knowledge if they are unavailable.

# Output constraints

- Use only plain Terraform filenames. For multiple components, organize by responsibility:
  `versions.tf`, `provider.tf`, `network.tf`, `compute.tf`, `database.tf`, `storage.tf`, and
  `outputs.tf` as needed. Terraform loads all `.tf` files together; do not duplicate blocks or
  create empty files.
- Use the user's confirmed AWS region and constrain `hashicorp/aws` to `~> 6.0`. Use
  `hashicorp/random ~> 3.0` only when random resources are present. Interpret `eu central`,
  `eu-central`, and `Frankfurt` as `eu-central-1`.
- Never write credentials, passwords, placeholders, `local-exec`, `remote-exec`, provisioners, or
  `data "external"`. Use AWS-managed RDS passwords, private subnets, encryption, and blocked S3
  public access. Production VPCs require flow logs and an empty default security group. Encrypted
  resources use customer-managed KMS keys with least-privilege policies; PostgreSQL uses
  `rds.force_ssl = 1`.
- When enabling RDS Enhanced Monitoring (`monitoring_interval`), always create an IAM role trusted
  by `monitoring.rds.amazonaws.com`, attach
  `service-role/AmazonRDSEnhancedMonitoringRole`, and set `monitoring_role_arn`. CloudWatch Logs
  log groups must use a dedicated KMS key whose policy permits the regional Logs service; never
  reuse an RDS-only KMS key for CloudWatch Logs.
- Do not enable EC2 termination protection by default. Omit `disable_api_termination` or set it to
  `false`, so an explicitly approved destroy can remove the instance. Only set it to `true` when
  the user explicitly requests termination protection and explain that destroy requires disabling it.
- Never hardcode an AMI id such as `ami-...`: AMI ids are region-specific and become stale. For
  EC2, define `data "aws_ami"` with `owners = ["amazon"]`, `most_recent = true`, filters for
  `state = "available"`, the requested architecture, and an Amazon Linux 2023 name; set
  `ami = data.aws_ami.<name>.id`.
- Do not open `0.0.0.0/0` unless explicitly requested. Do not add S3 replication, access logging,
  or event notifications only for generic Checkov recommendations. `CKV_AWS_18`, `CKV_AWS_144`,
  and `CKV2_AWS_62` are accepted findings; other actionable findings are blocking.

# Behavioral guidelines

- Treat each message as a request against the current workspace. Preserve unrelated resources,
  read existing files, and edit only what the user asks for. A follow-up is an amendment, not a
  validation-only request.
- If no region is supplied and no existing provider region is available, ask one question and write
  nothing. Otherwise act immediately. Never claim a change was made unless a file was written.
- After writing, call `check_infrastructure`. Fix HCL validation or security failures and check again,
  up to five attempts. Stop and explain when Terraform, provider downloads, or AWS credentials make
  a safe check impossible.
- Never finish with "I can fix this next" while blocking Checkov findings remain. You must edit the
  Terraform files and call `check_infrastructure` again; only stop after the scan passes or a
  non-retryable infrastructure error is reported.
- For `CKV_AWS_394` on `aws_availability_zones`, do not keep selecting
  `data.aws_availability_zones.*.names[index]`; use explicitly pinned Availability Zone names
  matching the confirmed region, or another fixed, deterministic list.
- Reply in the user's latest language. State what changed, the important security choices, and any
  unresolved error. Chat never applies or destroys infrastructure; those actions require approval.

# Examples

**Example 1 — create**
User: "Create a private encrypted S3 bucket in eu-central-1."
Action: write the required provider and storage files, then call `check_infrastructure`.
Reply: "Created the private encrypted bucket in eu-central-1. Public access is blocked."

**Example 2 — follow-up edit**
Existing workspace contains `provider.tf` and a private bucket. User: "Enable versioning."
Action: preserve the provider and security settings, edit the responsible Terraform file, then
call `check_infrastructure`. Do not regenerate unrelated resources.

**Example 3 — region amendment**
Existing workspace uses `eu-central-1`. User: "Change the region to us-east-1."
Action: update the existing AWS provider configuration to `us-east-1`, preserve all other resources,
and validate the workspace. Do not merely say that the region was changed."""

_MAX_VALIDATE = 2
class AgentCancelled(Exception):
    """Raised when a user cancels an active agent job."""
_TERMINATION_PROTECTION_REQUEST = re.compile(
    r"(?:termination protection|ochron(?:a|ę) terminacji|blokad(?:a|ę) usuni(?:ę|e)cia)",
    re.IGNORECASE,
)
_TERMINATION_PROTECTION_TRUE = re.compile(
    r"disable_api_termination\s*=\s*true",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class AgentTurn:
    reply: str
    files: list[str]
    validation: CommandResult
    plan: CommandResult
    scan: CommandResult
    registry_docs: bool


class InfraAgent:
    """Writes session Terraform files with OpenAI and validates them."""

    def __init__(self, workspace: Path, settings: Settings) -> None:
        self.workspace = workspace
        self.settings = settings

    def run(
        self,
        message: str,
        on_progress: Callable[[str, str, str | None], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AgentTurn:
        with workspace_lease(self.workspace):
            return self._run_unlocked(message, on_progress, cancel_event)

    def _run_unlocked(
        self,
        message: str,
        on_progress: Callable[[str, str, str | None], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> AgentTurn:
        progress = on_progress or (lambda _stage, _message, _command=None: None)
        cancel_event = cancel_event or threading.Event()

        def ensure_active() -> None:
            if cancel_event.is_set():
                raise AgentCancelled("Agent job cancelled by the user.")

        progress("generating", "Generating Terraform configuration...", "AI agent / Terraform tools")
        timing = Timing()
        scanner = WorkspaceScanner(self.workspace)
        terraform = TerraformService(self.workspace, self.settings, timing=timing)
        checkov = CheckovService(self.workspace)
        termination_protection_requested = bool(_TERMINATION_PROTECTION_REQUEST.search(message))
        reviewer = ReviewAgent(
            self.workspace,
            terraform,
            checkov,
            progress,
            _command_message,
            _MAX_VALIDATE,
            timing,
        )

        def write_tf(filename: str, content: str) -> str:
            """Write one .tf file in the session directory."""
            ensure_active()
            name = Path(filename).name
            if name != filename or not name.endswith(".tf") or name in {".tf"}:
                return "Rejected: only a plain *.tf filename is allowed."
            rejected = rejected_hcl(content)
            if rejected:
                return rejected
            if (
                _TERMINATION_PROTECTION_TRUE.search(content)
                and not termination_protection_requested
            ):
                return (
                    "Rejected: EC2 termination protection is disabled by default. "
                    "Only enable disable_api_termination after the user explicitly "
                    "requests termination protection."
                )
            target = (self.workspace / name).resolve()
            if target.parent != self.workspace.resolve():
                return "Rejected: path escapes the session directory."
            if len(content.encode("utf-8")) > 200_000:
                return "Rejected: file is larger than 200 KB."
            if target.is_file() and target.read_text(encoding="utf-8") == content:
                return f"{name} is unchanged; reuse the existing infrastructure review."
            target.write_text(content, encoding="utf-8")
            return f"Wrote {name}."

        def read_workspace() -> str:
            """Read the .tf files already in this session."""
            ensure_active()
            existing = scanner.read()
            return existing or "Workspace is empty."

        def check_infrastructure() -> str:
            """Validate the files, scan them, and run terraform plan when AWS credentials exist."""
            return reviewer.check(ensure_active)

        conversation_path = self.workspace / ".agent-conversation.json"
        conversation = _read_conversation(conversation_path)
        existing = scanner.read()
        region = _detect_region(message)
        existing_region = _provider_region(existing)
        region_update = _update_provider_region(self.workspace, region) if region and existing else None
        context = ["# Session context", ""]
        if region:
            context.append(
                f"Confirmed AWS region: {region}. Do not ask for the region again; use it in provider.tf."
            )
            if region_update:
                context.append(region_update)
        elif existing_region:
            context.append(
                f"Existing AWS provider region: {existing_region}. Keep using it unless the user explicitly requests a change."
            )
        else:
            context.append("No AWS region is supplied by the user or existing provider. Ask once before writing files.")
            reply = "Which AWS region should I create the server in? For example: `eu-central-1` (Frankfurt) or `us-east-1`."
            _write_conversation(
                conversation_path,
                [*conversation, {"role": "user", "content": message}, {"role": "assistant", "content": reply}],
            )
            progress("completed", "Waiting for the AWS region.", None)
            return AgentTurn(
                reply=reply,
                files=scanner.files(),
                validation=CommandResult(
                    ok=True,
                    command="region selection",
                    stdout="",
                    stderr="",
                    returncode=0,
                ),
                plan=CommandResult(
                    ok=True,
                    command="terraform plan",
                    stdout="",
                    stderr="",
                    returncode=0,
                ),
                scan=CommandResult(
                    ok=True,
                    command="checkov",
                    stdout="",
                    stderr="",
                    returncode=0,
                ),
                registry_docs=False,
            )
        if existing:
            context.append(f"Existing Terraform files: {', '.join(scanner.files())}")
            context.append(
                "This is a follow-up against the existing workspace. Inspect the content below, make the requested edit, "
                "and validate it. Do not only describe a possible fix.\n\n"
                f"{_model_output(existing, 12000)}"
            )
        else:
            context.append("The session has no Terraform files yet.")
        history_context = ""
        if conversation:
            history_context = (
                "\n\n# Recent conversation history\n"
                "Use this history to resolve short follow-ups such as a region answer. "
                "The latest user request still has priority.\n"
                + "\n".join(
                    f"{item['role']}: {item['content']}"
                    for item in conversation[-6:]
                )
            )
        user_text = f"{message}\n\n" + "\n\n".join(context) + history_context
        fast_path = _is_fast_request(message, scanner.files())
        if fast_path:
            user_text += (
                "\n\n# Fast path\n"
                "This is a simple request. Do not use Terraform Registry tools. "
                "Write the required files and call check_infrastructure exactly once. "
                "Do not perform a repair pass unless the files were actually changed "
                "after a failed review."
            )

        async def invoke_graph() -> tuple[dict, bool]:
            ensure_active()
            model = ChatOpenAI(
                model=self.settings.openai_model,
                api_key=self.settings.openai_api_key,
            )
            planner = PlannerAgent(self.settings)
            builder = BuilderAgent(model, _SYSTEM_PROMPT, _MAX_VALIDATE)
            use_registry = _should_use_registry_tools(message, existing) and not fast_path
            if fast_path:
                registry_tools, registry_docs, registry_client = [], False, None
                planner_brief = ""
            else:
                (
                    planner_brief,
                    registry_tools,
                    registry_docs,
                    registry_client,
                ) = await planner.plan(user_text, use_registry, timing)
            builder_text = (
                f"{user_text}\n\n# Planner Agent brief\n{_model_output(planner_brief, 5000)}"
                if planner_brief
                else user_text
            )
            try:
                tools = [write_tf, read_workspace, check_infrastructure, *registry_tools]
                result = await builder.run(
                    prompt=builder_text,
                    tools=tools,
                    fallback_tools=[write_tf, read_workspace, check_infrastructure],
                    fast_path=fast_path,
                    files_digest=lambda: files_digest(self.workspace),
                    review_state=reviewer.current,
                    should_stop=lambda review: reviewer.exhausted
                    or non_retryable(*review) is not None,
                    model_output=_model_output,
                    timing=timing,
                )
                return result, registry_docs
            except RuntimeError as exc:
                if str(exc).strip().lower() != "event loop is closed":
                    raise
                result = await builder.run(
                    prompt=builder_text,
                    tools=[write_tf, read_workspace, check_infrastructure],
                    fallback_tools=[write_tf, read_workspace, check_infrastructure],
                    fast_path=True,
                    files_digest=lambda: files_digest(self.workspace),
                    review_state=reviewer.current,
                    should_stop=lambda review: reviewer.exhausted
                    or non_retryable(*review) is not None,
                    model_output=_model_output,
                    timing=timing,
                )
                return result, False
            finally:
                _ = registry_client

        use_registry = _should_use_registry_tools(message, existing) and not fast_path
        if not use_registry:
            model = ChatOpenAI(
                model=self.settings.openai_model,
                api_key=self.settings.openai_api_key,
            )
            builder = BuilderAgent(model, _SYSTEM_PROMPT, _MAX_VALIDATE)
            result = builder.run_sync(
                prompt=user_text,
                tools=[write_tf, read_workspace, check_infrastructure],
                fallback_tools=[write_tf, read_workspace, check_infrastructure],
                fast_path=fast_path,
                files_digest=lambda: files_digest(self.workspace),
                review_state=reviewer.current,
                should_stop=lambda review: reviewer.exhausted
                or non_retryable(*review) is not None,
                model_output=_model_output,
                timing=timing,
            )
            registry_docs = False
        else:
            try:
                result, registry_docs = asyncio.run(invoke_graph())
            except GraphRecursionError:
                progress("reviewing", "Finishing the review after the iteration limit...", "Terraform and Checkov review")
                result = {"messages": []}
                registry_docs = False
        progress("reviewing", "Reviewing the generated infrastructure...", "Terraform and Checkov review")
        if scanner.files() and reviewer.current() is not None and reviewer.state.digest == files_digest(self.workspace):
            validation, plan, scan = reviewer.current()
        elif scanner.files():
            validation, plan, scan = reviewer.final(ensure_active)
        else:
            validation = CommandResult(
                ok=False,
                command="terraform validate",
                stdout="",
                stderr="No Terraform files were written.",
                returncode=None,
            )
            plan = CommandResult(
                ok=False,
                command="terraform plan",
                stdout="",
                stderr="Skipped because no Terraform files were written.",
                returncode=None,
            )
            scan = _skipped_scan("Skipped because no Terraform files were written.")
        reply = _last_reply(result["messages"])
        review_passed = validation.ok and scan.ok and (plan.ok or credentials_missing(plan))
        if not review_passed:
            reply = (
                "Infrastructure files were generated, but the final Terraform review did not pass. "
                "No changes were applied. Review the reported validation, Checkov, or plan errors "
                "before retrying.\n\n"
                + (reply or "The agent stopped after the review limit.")
            )
        if not reply:
            reply = (
                "The infrastructure files were generated, but the agent reached its "
                "maximum reasoning steps before completing all security fixes. "
                "The current Terraform remains available for review; no changes were applied."
            )
        if not registry_docs:
            reply = reply.strip()
        _write_conversation(
            conversation_path,
            [*conversation, {"role": "user", "content": message}, {"role": "assistant", "content": reply}],
        )
        progress("completed", "Infrastructure review completed.", None)
        metrics = timing.report()
        progress(
            "metrics",
            "Timing: " + ", ".join(f"{name} {value} ms" for name, value in metrics.items()),
            None,
        )
        return AgentTurn(
            reply=reply,
            files=scanner.files(),
            validation=validation,
            plan=plan,
            scan=scan,
            registry_docs=registry_docs,
        )


def _skipped_scan(reason: str) -> CommandResult:
    return CommandResult(
        ok=False,
        command="checkov",
        stdout="",
        stderr=reason,
        returncode=None,
    )


from app.services.agent_context import (
    detect_region as _detect_region,
    provider_region as _provider_region,
    read_conversation as _read_conversation,
    update_provider_region as _update_provider_region,
    write_conversation as _write_conversation,
)


def _model_output(text: str, limit: int = 3000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated for model context]"


def _command_message(command: str) -> str:
    if command.startswith("terraform fmt"):
        return "Formatting the Terraform configuration..."
    if command.startswith("terraform init"):
        return "Initializing Terraform providers..."
    if command.startswith("terraform validate"):
        return "Validating the Terraform configuration..."
    if command.startswith("terraform plan"):
        return "Creating the Terraform plan..."
    return "Running an infrastructure command..."


def _should_use_registry_tools(message: str, existing: str) -> bool:
    """Use MCP only when the user explicitly needs registry documentation."""
    normalized = message.lower()
    documentation_terms = (
        "document",
        "documentation",
        "registry",
        "provider",
        "resource type",
        "jak skonfigurow",
        "jak uży",
        "jak uzy",
    )
    return any(term in normalized for term in documentation_terms)


def _is_fast_request(message: str, files: list[str]) -> bool:
    """Identify small resource requests that do not need MCP or repair rounds."""
    if any(name not in {"provider.tf", "versions.tf"} for name in files):
        return False
    normalized = message.lower()
    resource_terms = (
        "bucket",
        "s3",
        "database",
        "rds",
        "queue",
        "sns",
        "dynamodb",
        "table",
    )
    words = re.findall(r"\w+", normalized)
    return len(words) <= 14 and any(term in normalized for term in resource_terms)


def _last_reply(messages: list) -> str:
    for message in reversed(messages):
        content = getattr(message, "content", "")
        tool_calls = getattr(message, "tool_calls", None)
        if tool_calls:
            continue
        text = _as_text(content).strip()
        if text and message.__class__.__name__ in {"AIMessage", "AIMessageChunk"}:
            return text
    return ""


def _as_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content)
