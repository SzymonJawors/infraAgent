import asyncio

from langchain_openai import ChatOpenAI

from app.services.registry import load_registry_tools
from app.services.timing import Timing


PLANNER_PROMPT = """You are the InfraAgent Planner.
Analyze the user's latest infrastructure request, recent conversation, AWS region,
and existing Terraform files. Return a short implementation brief for a separate
Builder Agent. Do not write Terraform, do not call tools, and do not ask questions.
Resolve short answers such as "eu-central 1" against the previous request.
List the requested resources, files to preserve, files to change, and essential
security requirements. If the request is only a clarification answer, connect it
to the original request instead of treating it as a new standalone task."""


class PlannerAgent:
    """Produces a read-only implementation brief and optional registry tools."""

    def __init__(self, settings) -> None:
        self.model = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
        )
        self.registry_url = settings.terraform_mcp_url

    async def plan(
        self,
        user_text: str,
        use_registry: bool,
        timing: Timing | None = None,
    ) -> tuple[str, list, bool, object | None]:
        timing = timing or Timing()
        planner_task = self.model.ainvoke(
            [
                {"role": "system", "content": PLANNER_PROMPT},
                {"role": "user", "content": user_text},
            ]
        )
        registry_task = (
            load_registry_tools(self.registry_url)
            if use_registry
            else _empty_registry_result()
        )
        with timing.measure("planner"):
            planner_result, registry_result = await asyncio.gather(planner_task, registry_task)
        if use_registry:
            timing.report()
        brief = _as_text(getattr(planner_result, "content", "")).strip()
        registry_tools, registry_docs, registry_client = registry_result
        return brief, registry_tools, registry_docs, registry_client


async def _empty_registry_result() -> tuple[list, bool, object | None]:
    return [], False, None


def _as_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block if isinstance(block, str) else str(block.get("text", ""))
            for block in content
            if isinstance(block, str) or isinstance(block, dict)
        )
    return str(content)
