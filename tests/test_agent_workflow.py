import unittest

from app.services.agent import _SYSTEM_PROMPT, _workspace_summary, _write_requires_workspace_read


class AgentWorkflowTests(unittest.TestCase):
    def test_write_requires_a_workspace_read(self) -> None:
        message = _write_requires_workspace_read(False)

        self.assertIsNotNone(message)
        self.assertIn("read_workspace()", message or "")

    def test_write_is_allowed_after_a_workspace_read(self) -> None:
        self.assertIsNone(_write_requires_workspace_read(True))

    def test_prompt_documents_read_before_write(self) -> None:
        self.assertIn("Always call `read_workspace()` before", _SYSTEM_PROMPT)

    def test_workspace_summary_is_compact_and_lists_addresses(self) -> None:
        terraform = '''
        provider "aws" { region = "eu-central-1" }
        resource "aws_vpc" "main" {}
        resource "aws_subnet" "private" {}
        data "aws_ami" "linux" {}
        '''

        summary = _workspace_summary(terraform, ["network.tf", "provider.tf"])

        self.assertIn("aws_vpc.main", summary)
        self.assertIn("data.aws_ami.linux", summary)
        self.assertIn("network.tf, provider.tf", summary)
        self.assertNotIn('provider "aws"', summary)


if __name__ == "__main__":
    unittest.main()
