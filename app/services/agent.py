from dataclasses import dataclass
from pathlib import Path

from langchain.agents import create_agent
from langchain_openai import ChatOpenAI

from app.core.config import Settings
from app.services.scanner import WorkspaceScanner
from app.services.terraform import CommandResult, TerraformService

_SYSTEM_PROMPT = """You are InfraAgent. You write Terraform (HCL) for AWS from the user's request.

Rules:
- Reply in the user's language.
- Write only files whose names end in .tf, with no directories.
- Keep editing the files already in the session. Do not start over unless the user asks.
- AWS credentials come from the process environment. Never put access keys, secret keys, or passwords in .tf files.
- Prefer private subnets, encryption at rest, and blocked public access.
- Do not open 0.0.0.0/0 unless the user explicitly demands a public endpoint, and then say so in the reply.
- After writing files, call validate_terraform.
- If validation fails because of HCL, fix the files and validate again. Stop after three validation attempts.
- If the CLI is missing or provider download fails, do not keep rewriting. Explain that in the reply.
- Do not run apply or destroy. Tell the user those wait for an explicit approval later.
"""

_MAX_VALIDATE = 3


@dataclass(frozen=True)
class AgentTurn:
    reply: str
    files: list[str]
    validation: CommandResult


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
            lowered = content.lower()
            if "aws_secret_access_key" in lowered or "aws_access_key_id" in lowered:
                return "Rejected: do not put AWS credentials in Terraform files."
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

        def validate_terraform() -> str:
            """Run terraform fmt, init -backend=false, and validate."""
            attempts["n"] += 1
            if attempts["n"] > _MAX_VALIDATE:
                return "Validation attempt limit reached. Stop and summarize the remaining error."
            result = terraform.check()
            status = "ok" if result.ok else "failed"
            if result.missing_binary:
                status = "terraform CLI missing"
            return f"{status}\n{result.command}\n{result.output()}".strip()

        model = ChatOpenAI(
            model=self.settings.openai_model,
            api_key=self.settings.openai_api_key,
        )
        graph = create_agent(
            model=model,
            tools=[write_tf, read_workspace, validate_terraform],
            system_prompt=_SYSTEM_PROMPT,
        )
        existing = scanner.read()
        user_text = message
        if existing:
            user_text = f"{message}\n\nExisting Terraform files:\n{existing}"
        result = graph.invoke(
            {"messages": [{"role": "user", "content": user_text}]},
            config={"recursion_limit": 20},
        )
        validation = (
            terraform.check()
            if scanner.files()
            else CommandResult(
                ok=False,
                command="terraform validate",
                stdout="",
                stderr="No Terraform files were written.",
                returncode=None,
            )
        )
        return AgentTurn(
            reply=_last_reply(result["messages"]),
            files=scanner.files(),
            validation=validation,
        )


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
