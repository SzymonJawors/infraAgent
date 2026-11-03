import unittest
from unittest.mock import AsyncMock, Mock

from app.services.agent_core import AgentCore


class AgentCoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_core_forwards_the_shared_tool_loop_contract(self) -> None:
        core = AgentCore(Mock(), "system", 2)
        core.builder.run = AsyncMock(return_value={"messages": []})

        result = await core.run(
            "request",
            ["read_workspace"],
            ["read_workspace"],
            False,
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            False,
        )

        self.assertEqual(result, {"messages": []})
        core.builder.run.assert_awaited_once()

    def test_sync_core_forwards_the_shared_tool_loop_contract(self) -> None:
        core = AgentCore(Mock(), "system", 2)
        core.builder.run_sync = Mock(return_value={"messages": []})

        result = core.run_sync(
            "request",
            ["read_workspace"],
            ["read_workspace"],
            False,
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            Mock(),
            False,
        )

        self.assertEqual(result, {"messages": []})
        core.builder.run_sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
