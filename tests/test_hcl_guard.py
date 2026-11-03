import unittest

from app.services.hcl_guard import rejected_hcl


class HclGuardTests(unittest.TestCase):
    def test_rejects_hardcoded_ami(self) -> None:
        reason = rejected_hcl('resource "aws_instance" "server" {\n  ami = "ami-0123456789abcdef0"\n}')
        self.assertIsNotNone(reason)
        self.assertIn("do not hardcode an AMI", reason or "")

    def test_allows_ami_data_source_reference(self) -> None:
        self.assertIsNone(
            rejected_hcl(
                'data "aws_ami" "amazon_linux" {}\n'
                'resource "aws_instance" "server" {\n'
                '  ami = data.aws_ami.amazon_linux.id\n'
                '}'
            )
        )
