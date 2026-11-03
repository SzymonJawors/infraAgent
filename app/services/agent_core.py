from collections.abc import Callable

from app.services.builder import BuilderAgent
from app.services.timing import Timing


class AgentCore:
    """Shared tool-loop runner used by production turns and evaluations."""

    def __init__(self, model, system_prompt: str, max_validate: int) -> None:
        self.builder = BuilderAgent(model, system_prompt, max_validate)

    async def run(
        self,
        prompt: str,
        tools: list,
        fallback_tools: list,
        fast_path: bool,
        files_digest: Callable[[], str],
        review_state: Callable,
        should_stop: Callable,
        model_output: Callable,
        timing: Timing,
        skip_checkov: bool,
    ):
        return await self.builder.run(
            prompt=prompt,
            tools=tools,
            fallback_tools=fallback_tools,
            fast_path=fast_path,
            files_digest=files_digest,
            review_state=review_state,
            should_stop=should_stop,
            model_output=model_output,
            timing=timing,
            skip_checkov=skip_checkov,
        )

    def run_sync(
        self,
        prompt: str,
        tools: list,
        fallback_tools: list,
        fast_path: bool,
        files_digest: Callable[[], str],
        review_state: Callable,
        should_stop: Callable,
        model_output: Callable,
        timing: Timing,
        skip_checkov: bool,
    ):
        return self.builder.run_sync(
            prompt=prompt,
            tools=tools,
            fallback_tools=fallback_tools,
            fast_path=fast_path,
            files_digest=files_digest,
            review_state=review_state,
            should_stop=should_stop,
            model_output=model_output,
            timing=timing,
            skip_checkov=skip_checkov,
        )
