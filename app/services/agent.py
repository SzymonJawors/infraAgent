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
from app.services.builder import BuilderAgent
from app.services.planner import PlannerAgent
from app.services.reviewer import ReviewAgent, credentials_missing, non_retryable
from app.services.timing import Timing
from app.services.workspace_lock import workspace_lease

_SYSTEM_PROMPT = """# Role
You are InfraAgent, a tool-using AWS Terraform agent. Translate the request into Terraform in the
current session workspace, validate it, and explain only what actually changed.

# Tools
- `write_tf(filename, content)` creates or replaces one plain `*.tf` file.
- `read_workspace()` reads the current Terraform files before a follow-up change.
- `check_infrastructure()` runs fmt, init, validate, Checkov, and plan when AWS credentials exist.
  It never applies or destroys infrastructure.
- Use Terraform Registry tools for provider documentation when available; continue from knowledge
  if they are unavailable.

# Terraform rules
- Use responsibility-based files (`versions.tf`, `provider.tf`, `network.tf`, `compute.tf`,
  `database.tf`, `storage.tf`, `outputs.tf`) without duplicates or empty files.
- Use the confirmed region, `hashicorp/aws ~> 6.0`, and `hashicorp/random ~> 3.0` only when needed.
  Treat `eu central`, `eu-central`, and `Frankfurt` as `eu-central-1`.
- Never write credentials, passwords, placeholders, provisioners, `local-exec`, `remote-exec`, or
  `data "external"`. Use private subnets, encryption, blocked S3 public access, customer-managed
  KMS keys with least-privilege policies, flow logs and an empty default security group for
  production VPCs, and PostgreSQL `rds.force_ssl = 1`.
- Lambda packages require an explicit `hashicorp/archive` declaration in `versions.tf`, a local
  deterministic source, and Terraform init may download it when registry access is available.
- RDS Enhanced Monitoring requires its trusted IAM role, the AWS managed monitoring policy, and
  `monitoring_role_arn`. CloudWatch Logs require a dedicated KMS key allowing the regional Logs
  service; do not reuse an RDS-only key.
- RDS subnet groups require two subnets in different AZs. Add a second private subnet and route
  association when an existing VPC has only one.
- Never guess `aws_db_instance.engine_version`; omit it unless the exact regional version is
  confirmed. Do not enable EC2 termination protection by default.
- Never hardcode AMI IDs. Use an `aws_ami` data source for the requested architecture and Amazon
  Linux 2023. Do not open `0.0.0.0/0` unless explicitly requested.
- Do not add replication, access logging, or event notifications only for generic Checkov findings.
  `CKV_AWS_18`, `CKV_AWS_144`, and `CKV2_AWS_62` are accepted; other actionable findings block.

# Workflow and response
- Treat every message as a request against the current workspace. Preserve unrelated resources and
  make only requested edits. Ask one region question and write nothing when no region is available.
- After writing, call `check_infrastructure`; fix HCL or security failures and recheck once more.
  Stop on non-retryable provider, Terraform, credential, or infrastructure errors.
- For `CKV_AWS_394`, use pinned AZ names matching the confirmed region, not indexed AZ data.
- Reply in the user's latest language. Chat never applies or destroys; those require approval.
- Keep the final reply short and current with exactly `Changed`, `Validation`, and `Next step` only
  when needed. Do not repeat old settings, reports, or optional follow-up offers."""

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
        skip_checkov: bool = False,
    ) -> AgentTurn:
        return self._run_unlocked(message, on_progress, cancel_event, skip_checkov)

    def _run_unlocked(
        self,
        message: str,
        on_progress: Callable[[str, str, str | None], None] | None = None,
        cancel_event: threading.Event | None = None,
        skip_checkov: bool = False,
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
            skip_checkov,
            preflight=lambda: _database_subnet_group_issue(scanner.read()),
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
            with workspace_lease(self.workspace):
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
            history_items: list[str] = []
            for item in conversation[-6:]:
                role = item.get("role")
                content = item.get("content", "").strip()
                if role not in {"user", "assistant"} or not content:
                    continue
                if len(content) > 1600:
                    content = content[:1600].rstrip() + "..."
                label = "User" if role == "user" else "Assistant"
                history_items.append(f"{label}: {content}")
            if history_items:
                history_context = (
                    "\n\n# Recent conversation context\n"
                    "Use this short history to understand follow-ups and prior decisions. "
                    "The latest request has priority. Existing Terraform files are the source "
                    "of truth; assistant messages are only context and may be stale. Do not "
                    "copy previous summaries, validation reports, security checklists, or "
                    "unchanged settings into the final reply. Report only what changed in "
                    "this turn and any current unresolved issue.\n\n"
                    + "\n\n".join(history_items)
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
                    skip_checkov=skip_checkov,
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
                    skip_checkov=skip_checkov,
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
                skip_checkov=skip_checkov,
            )
            registry_docs = False
        else:
            try:
                result, registry_docs = asyncio.run(invoke_graph())
            except GraphRecursionError:
                progress(
                    "reviewing",
                    "Finishing the review after the iteration limit...",
                    "Terraform review" if skip_checkov else "Terraform and Checkov review",
                )
                result = {"messages": []}
                registry_docs = False
        progress(
            "reviewing",
            "Reviewing the generated infrastructure...",
            "Terraform review" if skip_checkov else "Terraform and Checkov review",
        )
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
        reply = _finalize_reply(reply)
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


def _database_subnet_group_issue(terraform: str) -> str | None:
    """Catch the AWS RDS two-AZ requirement before approval/apply."""
    if re.search(
        r'resource\s+"aws_db_instance"\s+"[^"]+"\s*\{(?:(?!\n\}).)*?^\s*engine_version\s*=',
        terraform,
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    ):
        return (
            "RDS engine_version is hardcoded to a possibly unavailable version. "
            "Remove engine_version or replace it only with a version confirmed "
            "available in the selected region and instance configuration."
        )
    for block in re.finditer(
        r'resource\s+"aws_db_subnet_group"\s+"[^"]+"\s*\{(.*?)\n\}',
        terraform,
        re.IGNORECASE | re.DOTALL,
    ):
        subnet_ids = re.search(r"subnet_ids\s*=\s*\[(.*?)\]", block.group(1), re.IGNORECASE | re.DOTALL)
        if not subnet_ids:
            continue
        references = re.findall(r"aws_subnet\.[A-Za-z0-9_]+\.id", subnet_ids.group(1))
        if len(references) == 1:
            return (
                "RDS DB subnet groups must include subnets in at least two Availability Zones. "
                f"Only {references[0]} is configured. Add a second private subnet in a "
                "different AZ and include both subnet IDs in subnet_ids before planning."
            )
    return None


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
        "server",
        "ec2",
        "instance",
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


_OPTIONAL_FOLLOW_UP = re.compile(
    r"(?im)^(?:[-*]\s*)?(?:if you want|if you would like|jeśli chcesz|jesli chcesz|"
    r"jeżeli chcesz|jezeli chcesz|mogę jeszcze|moge jeszcze|mogę też|moge tez)\b"
)


def _finalize_reply(reply: str) -> str:
    """Keep agent replies focused on the completed result and required action."""
    paragraphs = re.split(r"\n\s*\n", reply.strip())
    for index, paragraph in enumerate(paragraphs):
        if _OPTIONAL_FOLLOW_UP.search(paragraph):
            paragraphs = paragraphs[:index]
            break
    return "\n\n".join(paragraphs).strip() or reply.strip()


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
