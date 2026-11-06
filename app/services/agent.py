import asyncio
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from langchain_openai import ChatOpenAI
from langgraph.errors import GraphRecursionError

from app.core.config import Settings
from app.services.checkov_scan import CheckovService
from app.services.hcl_guard import (
    align_hcl,
    apply_version_overrides,
    engine_version_problem,
    rejected_hcl,
    requested_engine_version,
    requested_provider_constraints,
)
from app.services.scanner import WorkspaceScanner
from app.services.approval import files_digest
from app.services.terraform import CommandResult, TerraformService
from app.services.agent_core import AgentCore
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
- Always call `read_workspace()` before the first `write_tf()` call, including for an empty
  workspace. A write attempted before that read is rejected; call `read_workspace()` and retry.

# Terraform rules
- Use responsibility-based files (`versions.tf`, `provider.tf`, `network.tf`, `compute.tf`,
  `database.tf`, `storage.tf`, `outputs.tf`) without duplicates or empty files.
- Use the confirmed region. Provider versions default to `hashicorp/aws ~> 6.0` and, only when
  needed, `hashicorp/random ~> 3.0`. If the user explicitly requests another provider version,
  write that exact constraint in `versions.tf`. A different value is rewritten before init.
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
- Never guess `aws_db_instance.engine_version`. Omit it unless the user explicitly requests an
  exact engine version, and then use only that value. Do not enable EC2 termination protection
  by default.
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
- Keep the final reply useful and concrete, not artificially short. For every meaningful change,
  explain: what was added or changed, why it was needed, and how it works or affects the result.
  Do not only list filenames or Checkov rule IDs. Prefer clear sections such as "What changed",
  "Why", "How it works", "Validation", and "Next step"; use as many of these as the result needs.
  Keep one blank line between sections, avoid blank lines between consecutive bullets, and do not
  repeat old settings, reports, or optional follow-up offers."""

_MAX_VALIDATE = 5
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
    tool_calls: list[str] = field(default_factory=list)


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
            preflight=lambda: _database_subnet_group_issue(scanner.read(), message),
        )
        workspace_read = False

        def write_tf(filename: str, content: str) -> str:
            """Write one .tf file in the session directory."""
            ensure_active()
            gate_error = _write_requires_workspace_read(workspace_read)
            if gate_error:
                return gate_error
            name = Path(filename).name
            if name != filename or not name.endswith(".tf") or name in {".tf"}:
                return "Rejected: only a plain *.tf filename is allowed."
            target = (self.workspace / name).resolve()
            if target.parent != self.workspace.resolve():
                return "Rejected: path escapes the session directory."
            previous = target.read_text(encoding="utf-8") if target.is_file() else ""
            content = align_hcl(content, message=message, filename=name, previous=previous)
            rejected = rejected_hcl(content, message=message, filename=name)
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
            if len(content.encode("utf-8")) > 200_000:
                return "Rejected: file is larger than 200 KB."
            if target.is_file() and target.read_text(encoding="utf-8") == content:
                return f"{name} is unchanged; reuse the existing infrastructure review."
            with workspace_lease(self.workspace):
                target.write_text(content, encoding="utf-8")
            return f"Wrote {name}."

        def read_workspace() -> str:
            """Read the .tf files already in this session."""
            nonlocal workspace_read
            ensure_active()
            workspace_read = True
            existing = scanner.read()
            return existing or "Workspace is empty."

        def sync_versions() -> list[str]:
            with workspace_lease(self.workspace):
                return apply_version_overrides(self.workspace, message)

        def check_infrastructure() -> str:
            """Validate the files, scan them, and run terraform plan when AWS credentials exist."""
            ensure_active()
            sync_versions()
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
                    ok=False,
                    command="terraform validate",
                    stdout="",
                    stderr="No Terraform files were written.",
                    returncode=None,
                ),
                plan=CommandResult(
                    ok=False,
                    command="terraform plan",
                    stdout="",
                    stderr="Skipped because no Terraform files were written.",
                    returncode=None,
                ),
                scan=_skipped_scan("Skipped because no Terraform files were written."),
                registry_docs=False,
            )
        corrected = sync_versions() if scanner.files() else []
        if corrected:
            existing = scanner.read()
            context.append(
                "Versions already stored in this session were corrected in: "
                + ", ".join(corrected)
                + "."
            )
        if existing:
            context.append(_workspace_summary(existing, scanner.files()))
            context.append(
                "This is a follow-up against the existing workspace. Call read_workspace() to inspect "
                "the full HCL, preserve unrelated resources, make the requested edit, and validate it. "
                "Do not only describe a possible fix."
            )
        else:
            context.append("The session has no Terraform files yet.")
        provider_constraints = requested_provider_constraints(message)
        if provider_constraints:
            rendered = ", ".join(
                f"hashicorp/{name} {constraint}" for name, constraint in provider_constraints.items()
            )
            context.append(
                f"Confirmed provider versions: {rendered}. "
                "These constraints are written into versions.tf before Terraform init. "
                "Do not change them back to the default."
            )
        engine_version = requested_engine_version(message)
        if engine_version:
            context.append(
                f"Confirmed RDS engine_version: {engine_version}. "
                "This exact value is written into aws_db_instance. Any other engine_version is replaced."
            )
        repository_context = _read_repository_context(self.workspace)
        if repository_context:
            context.append(repository_context)
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
                "Write the required files and call check_infrastructure once initially. "
                "If validation or Checkov reports a blocking finding, perform one repair pass, "
                "then call check_infrastructure again before finishing."
            )

        async def invoke_graph() -> tuple[dict, bool]:
            ensure_active()
            model = ChatOpenAI(
                model=self.settings.openai_model,
                api_key=self.settings.openai_api_key,
            )
            planner = PlannerAgent(self.settings)
            core = AgentCore(model, _SYSTEM_PROMPT, _MAX_VALIDATE)
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
                result = await core.run(
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
                result = await core.run(
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
            core = AgentCore(model, _SYSTEM_PROMPT, _MAX_VALIDATE)
            try:
                result = core.run_sync(
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
            except GraphRecursionError:
                progress(
                    "reviewing",
                    "Finishing the review after the iteration limit...",
                    "Terraform review" if skip_checkov else "Terraform and Checkov review",
                )
                result = {"messages": []}
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
        if scanner.files():
            sync_versions()
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
            tool_calls=_tool_calls(result),
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


def _tool_calls(result: object) -> list[str]:
    messages = result.get("messages", []) if isinstance(result, dict) else []
    calls: list[str] = []
    for message in messages:
        raw_calls = (
            message.get("tool_calls", [])
            if isinstance(message, dict)
            else getattr(message, "tool_calls", [])
        )
        for call in raw_calls or []:
            if isinstance(call, dict):
                name = call.get("name") or call.get("function", {}).get("name")
            else:
                name = getattr(call, "name", None)
            if name:
                calls.append(str(name))
    return calls


def _write_requires_workspace_read(workspace_read: bool) -> str | None:
    if workspace_read:
        return None
    return (
        "Rejected: read_workspace() must be called before write_tf(). "
        "Read the current Terraform workspace first, then retry the write."
    )


def _workspace_summary(terraform: str, filenames: list[str]) -> str:
    addresses = sorted(set(
        f"{kind}.{resource_type}.{name}"
        for kind, resource_type, name in re.findall(
            r'^\s*(resource|data)\s+"([^"]+)"\s+"([^"]+)"\s*\{',
            terraform,
            re.MULTILINE,
        )
    ))
    modules = sorted(set(re.findall(r'^\s*module\s+"([^"]+)"\s*\{', terraform, re.MULTILINE)))
    providers = sorted(set(re.findall(r'^\s*provider\s+"([^"]+)"\s*\{', terraform, re.MULTILINE)))
    lines = len(terraform.splitlines())
    summary = [
        f"Existing Terraform workspace: {len(filenames)} file(s), {lines} line(s).",
        f"Files: {', '.join(filenames)}.",
        f"Resources: {', '.join(addresses) if addresses else 'none detected'}.",
        f"Modules: {', '.join(modules) if modules else 'none detected'}.",
        f"Providers: {', '.join(providers) if providers else 'none detected'}.",
    ]
    return "\n".join(summary)


def _read_repository_context(workspace: Path) -> str:
    path = workspace / ".github-repository-analysis.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    if not isinstance(payload, dict):
        return ""
    repository = str(payload.get("repository") or "").strip()
    if not repository:
        return ""
    technologies = payload.get("technologies")
    technology_text = ", ".join(
        str(item) for item in technologies if isinstance(item, str)
    ) if isinstance(technologies, list) else "none detected"
    terraform_files = payload.get("terraform_files")
    terraform_text = ", ".join(
        str(item) for item in terraform_files[:30] if isinstance(item, str)
    ) if isinstance(terraform_files, list) else "none"
    readme = str(payload.get("readme_excerpt") or "").strip()
    if len(readme) > 2500:
        readme = readme[:2500].rstrip() + "..."
    return (
        "# GitHub repository analysis\n"
        f"Repository: {repository}\n"
        f"Detected technologies: {technology_text}\n"
        f"Terraform files: {terraform_text}\n"
        f"Recommendation: {payload.get('recommendation', '')}\n"
        "Use this as bounded architecture context only. Do not claim to have inspected files "
        "that are not listed here, and do not write credentials or application source code.\n"
        f"README excerpt:\n{readme or '(not available)'}"
    )


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


def _database_subnet_group_issue(terraform: str, message: str = "") -> str | None:
    """Catch the AWS RDS two-AZ requirement before approval/apply."""
    engine_issue = engine_version_problem(terraform, message)
    if engine_issue:
        return engine_issue
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
    compact = re.sub(r"[ \t]+\n", "\n", reply.strip())
    compact = re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", compact)
    paragraphs = re.split(r"\n\s*\n", compact)
    for index, paragraph in enumerate(paragraphs):
        if _OPTIONAL_FOLLOW_UP.search(paragraph):
            paragraphs = paragraphs[:index]
            break
    return "\n\n".join(paragraphs).strip() or compact


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
