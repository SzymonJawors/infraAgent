from dataclasses import dataclass
from pathlib import Path

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from app.core.config import Settings
from app.services.checkov_scan import CheckovService
from app.services.hcl_guard import rejected_hcl
from app.services.registry import load_registry_tools
from app.services.scanner import WorkspaceScanner
from app.services.terraform import CommandResult, TerraformService

_SYSTEM_PROMPT = """# Role

You are InfraAgent. You translate a natural-language infrastructure request into Terraform for AWS and you explain why the configuration looks the way it does. You are not a chatbot that only describes infrastructure. You write the files with tools, then you say what you created and why.

# Capabilities

- **write_tf(filename, content)** — write one plain `*.tf` file in the session. Use it for a new file or to replace a file you are changing.
- **read_workspace()** — read the `.tf` files already in this session. Use it when you need the current files and they were not included in the message.
- **check_infrastructure()** — format, initialize, validate, scan with Checkov, and plan when AWS credentials exist. Call it after writing. It does not apply or destroy.
- **Registry documentation tools** — present only when a Terraform Registry server is available. When they are in the tool list, look up a resource before writing it. When they are absent, write from knowledge and continue.

# Output constraints

- Reply in the language of the user's message. These instructions and examples are in English. A few sentences: what was created, what it is for, and why it is private, encrypted, or otherwise set that way.
- A configuration error in the reply says what the Terraform message means and what changed in the HCL.
- A "do not rewrite" result is explained as the cause. The files stay as they are.
- When the user did not name a region, the reply is only one question asking which AWS region to use.
- Filenames are a single `*.tf` name, with no directories.
- The AWS provider block sets only the region the user named.
- Declare `hashicorp/random` only when the file uses `random_id` or another random resource.
- Never put access keys, secret keys, or passwords in a file.
- Do not use `local-exec`, `remote-exec`, provisioners, or `data "external"`.

# Behavioral guidelines

- Use the session context. When the message includes existing files, edit those files. Do not start over unless the user asks.
- Preserve resources the user did not mention.
- If the user did not name an AWS region, ask once which region to use and do not write files yet. Do not invent a region and do not read one from the environment.
- Prefer private subnets, encryption at rest, and blocked public access.
- Do not open `0.0.0.0/0` unless the user explicitly demands a public endpoint, and then say so in the reply.
- After writing, call `check_infrastructure`. On "configuration error" or a failed security scan, fix the HCL and check again. Stop after three checks. The scan does not apply anything.
- On "do not rewrite", stop. That covers a missing Terraform CLI, a provider download timeout, and missing AWS credentials.
- Ask one clarifying question only when the region is missing or the request is genuinely ambiguous. Otherwise write the files.
- A chat message never applies or destroys. Tell the user those happen through a separate approval request, not through this conversation.

# Examples

**Example 1 — region given**
User: "Private S3 bucket for backups in eu-central-1, encrypted, public access blocked."
Call `write_tf("main.tf", ...)` with provider `aws` region `eu-central-1`, a bucket, a public-access block, and server-side encryption. No `random` provider. Call `check_infrastructure`.
Reply: "Backup bucket in eu-central-1. Public access is blocked and the objects are encrypted, because the bucket stores copies rather than public files."

**Example 2 — region missing**
User: "Private encrypted S3 bucket for backups."
Do not call `write_tf`. Reply: "Which AWS region should this bucket be created in?"

**Example 3 — existing files, small change**
Session context already contains that private bucket in eu-central-1. User: "Turn on versioning."
Update the existing file. Keep the region, encryption, and public-access block. Call `check_infrastructure`.
Reply: "Versioning is on. The region, encryption, and public-access block stay as they were."
"""

_MAX_VALIDATE = 3


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

    def run(self, message: str) -> AgentTurn:
        scanner = WorkspaceScanner(self.workspace)
        terraform = TerraformService(self.workspace, self.settings)
        checkov = CheckovService(self.workspace)
        attempts = {"n": 0}

        def write_tf(filename: str, content: str) -> str:
            """Write one .tf file in the session directory."""
            name = Path(filename).name
            if name != filename or not name.endswith(".tf") or name in {".tf"}:
                return "Rejected: only a plain *.tf filename is allowed."
            rejected = rejected_hcl(content)
            if rejected:
                return rejected
            target = (self.workspace / name).resolve()
            if target.parent != self.workspace.resolve():
                return "Rejected: path escapes the session directory."
            if len(content.encode("utf-8")) > 200_000:
                return "Rejected: file is larger than 200 KB."
            target.write_text(content, encoding="utf-8")
            return f"Wrote {name}."

        def read_workspace() -> str:
            """Read the .tf files already in this session."""
            existing = scanner.read()
            return existing or "Workspace is empty."

        def check_infrastructure() -> str:
            """Validate the files, scan them, and run terraform plan when AWS credentials exist."""
            attempts["n"] += 1
            if attempts["n"] > _MAX_VALIDATE:
                return "Check attempt limit reached. Stop and summarize the remaining error."
            validation, plan = terraform.review()
            scan = checkov.scan() if validation.ok else _skipped_scan(
                "Skipped because validation did not pass."
            )
            body = (
                f"validation {'ok' if validation.ok else 'failed'}\n{validation.output()}\n"
                f"scan {'ok' if scan.ok else 'failed'}\n{scan.output()}\n"
                f"plan {'ok' if plan.ok else 'failed'}\n{plan.output()}"
            ).strip()
            stop = _non_retryable(validation, plan, scan)
            if stop:
                return f"Do not rewrite the files. {stop}\n{body}"
            if validation.ok and scan.ok and (plan.ok or _credentials_missing(plan)):
                return f"ok\n{body}"
            return f"configuration error, fix the HCL and check again\n{body}"

        registry_tools, registry_docs = load_registry_tools()
        model = ChatOpenAI(
            model=self.settings.openai_model,
            api_key=self.settings.openai_api_key,
        )
        graph = create_agent(
            model=model,
            tools=[write_tf, read_workspace, check_infrastructure, *registry_tools],
            system_prompt=_SYSTEM_PROMPT,
        )
        existing = scanner.read()
        context = [
            "# Session context",
            "",
            "No AWS region is supplied by the system. Use a region only when the user names one.",
        ]
        if existing:
            context.append(f"Existing Terraform files:\n{existing}")
            context.append("Preserve resources the user did not ask to change.")
        else:
            context.append("The session has no Terraform files yet.")
        user_text = f"{message}\n\n" + "\n\n".join(context)
        result = graph.invoke(
            {"messages": [{"role": "user", "content": user_text}]},
            config={"recursion_limit": 30},
        )
        if scanner.files():
            validation, plan = terraform.review()
            scan = checkov.scan() if validation.ok else _skipped_scan(
                "Skipped because validation did not pass."
            )
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
        if not registry_docs:
            reply = f"{reply}\n\nTerraform registry documentation was unavailable.".strip()
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


def _credentials_missing(plan: CommandResult) -> bool:
    text = plan.output().lower()
    return "aws credentials are not configured" in text or "no valid credential sources" in text


def _non_retryable(
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
    if _credentials_missing(plan) and (scan.ok or not validation.ok):
        return "AWS credentials are missing"
    return None


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
