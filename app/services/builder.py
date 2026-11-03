from langchain.agents import create_agent
from app.services.timing import Timing


class BuilderAgent:
    """Runs the write-capable Terraform agent and its bounded repair passes."""

    def __init__(self, model, system_prompt: str, max_validate: int = 5) -> None:
        self.model = model
        self.system_prompt = system_prompt
        self.max_validate = max_validate

    async def run(
        self,
        prompt: str,
        tools: list,
        fallback_tools: list,
        fast_path: bool,
        files_digest,
        review_state,
        should_stop,
        model_output,
        timing: Timing | None = None,
    ):
        timing = timing or Timing()
        graph = create_agent(
            model=self.model,
            tools=tools,
            system_prompt=self.system_prompt,
        )
        with timing.measure("builder"):
            result = await self._invoke(graph, prompt, fallback_tools)
        for _ in range(0 if fast_path else self.max_validate - 1):
            last_review = review_state()
            if not last_review or last_review[2].ok or should_stop(last_review):
                break
            before_repair_digest = files_digest()
            findings = model_output(last_review[2].output(), 8000)
            repair_prompt = (
                f"{prompt}\n\nMANDATORY REPAIR PASS:\nThe latest Checkov scan still has "
                f"blocking findings. Do not summarize or ask the user. Read the current workspace, "
                f"edit the Terraform files to fix every finding below, then call check_infrastructure "
                f"again. Continue until the scan passes or a non-retryable error is reported.\n\n{findings}"
            )
            with timing.measure("builder_repair"):
                result = await self._invoke(graph, repair_prompt, fallback_tools)
            if files_digest() == before_repair_digest:
                break
        return result

    def run_sync(
        self,
        prompt: str,
        tools: list,
        fallback_tools: list,
        fast_path: bool,
        files_digest,
        review_state,
        should_stop,
        model_output,
        timing: Timing | None = None,
    ):
        timing = timing or Timing()
        graph = create_agent(
            model=self.model,
            tools=tools,
            system_prompt=self.system_prompt,
        )
        with timing.measure("builder"):
            result = self._invoke_sync(graph, prompt, fallback_tools)
        for _ in range(0 if fast_path else self.max_validate - 1):
            last_review = review_state()
            if not last_review or last_review[2].ok or should_stop(last_review):
                break
            before_repair_digest = files_digest()
            findings = model_output(last_review[2].output(), 8000)
            repair_prompt = (
                f"{prompt}\n\nMANDATORY REPAIR PASS:\nThe latest Checkov scan still has "
                f"blocking findings. Read the current workspace, edit the Terraform files, "
                f"then call check_infrastructure again.\n\n{findings}"
            )
            with timing.measure("builder_repair"):
                result = self._invoke_sync(graph, repair_prompt, fallback_tools)
            if files_digest() == before_repair_digest:
                break
        return result

    async def _invoke(self, graph, prompt: str, fallback_tools: list):
        try:
            return await graph.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": 24},
            )
        except RuntimeError as exc:
            if str(exc).strip().lower() != "event loop is closed":
                raise
            fallback_graph = create_agent(
                model=self.model,
                tools=fallback_tools,
                system_prompt=self.system_prompt,
            )
            return await fallback_graph.ainvoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": 24},
            )

    def _invoke_sync(self, graph, prompt: str, fallback_tools: list):
        try:
            return graph.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": 24},
            )
        except RuntimeError as exc:
            if str(exc).strip().lower() != "event loop is closed":
                raise
            fallback_graph = create_agent(
                model=self.model,
                tools=fallback_tools,
                system_prompt=self.system_prompt,
            )
            return fallback_graph.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": 24},
            )
