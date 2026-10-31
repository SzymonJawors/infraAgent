from dataclasses import dataclass
from pathlib import Path

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from app.core.config import Settings
from app.services.hcl_guard import rejected_hcl
from app.services.registry import load_registry_tools
from app.services.scanner import WorkspaceScanner
from app.services.terraform import CommandResult, TerraformService

_SYSTEM_PROMPT = """You are InfraAgent. You write Terraform (HCL) for AWS from the user's request.

Rules:
- Reply in the user's language.
- When registry documentation tools are available, look up the resource before writing it.
- Write only files whose names end in .tf, with no directories.
- Keep editing the files already in the session. Do not start over unless the user asks.
- AWS credentials stay in the Terraform process environment. Never put access keys, secret keys, or passwords in .tf files.
- Do not use local-exec, remote-exec, provisioner, or external data sources.
- Prefer private subnets, encryption at rest, and blocked public access.
- Do not open 0.0.0.0/0 unless the user explicitly demands a public endpoint, and then say so in the reply.
- Declare every provider you use in required_providers, including hashicorp/random when you use random_id.
- After writing files, call check_infrastructure.
- If the result says configuration error, fix the HCL and check again. Stop after three checks.
- If the result says do not rewrite, explain that error and stop.
- Do not run apply or destroy. Tell the user those wait for an explicit approval later.
"""

_MAX_VALIDATE = 3


@dataclass(frozen=True)
class AgentTurn:
    reply: str
    files: list[str]
    validation: CommandResult
    plan: CommandResult
    registry_docs: bool


class InfraAgent:
    """Writes session Terraform files with OpenAI and validates them."""

    def __init__(self, workspace: Path, settings: Settings) -> None:
        self.workspace = workspace
        self.settings = settings

    def run(self, message: str) -> AgentTurn:
        scanner = WorkspaceScanner(self.workspace)
        terraform = TerraformService(self.workspace, self.settings)
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
            """Validate the files and run terraform plan when AWS credentials exist."""
            attempts["n"] += 1
            if attempts["n"] > _MAX_VALIDATE:
                return "Check attempt limit reached. Stop and summarize the remaining error."
            validation, plan = terraform.review()
            body = (
                f"validation {'ok' if validation.ok else 'failed'}\n{validation.output()}\n"
                f"plan {'ok' if plan.ok else 'failed'}\n{plan.output()}"
            ).strip()
            stop = _non_retryable(validation, plan)
            if stop:
                return f"Do not rewrite the files. {stop}\n{body}"
            if validation.ok and plan.ok:
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
        user_text = message
        if existing:
            user_text = f"{message}\n\nExisting Terraform files:\n{existing}"
        result = graph.invoke(
            {"messages": [{"role": "user", "content": user_text}]},
            config={"recursion_limit": 30},
        )
        if scanner.files():
            validation, plan = terraform.review()
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
        reply = _last_reply(result["messages"])
        if not registry_docs:
            reply = f"{reply}\n\nTerraform registry documentation was unavailable.".strip()
        return AgentTurn(
            reply=reply,
            files=scanner.files(),
            validation=validation,
            plan=plan,
            registry_docs=registry_docs,
        )


def _non_retryable(validation: CommandResult, plan: CommandResult) -> str | None:
    text = f"{validation.output()}\n{plan.output()}".lower()
    if validation.missing_binary:
        return "terraform CLI is missing"
    if "timed out" in text:
        return "provider download timed out"
    if "aws credentials are not configured" in text or "no valid credential sources" in text:
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
