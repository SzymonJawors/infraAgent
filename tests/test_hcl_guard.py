import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from app.services.hcl_guard import align_hcl, apply_version_overrides, rejected_hcl


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

    def test_rejects_unverified_rds_engine_version(self) -> None:
        reason = rejected_hcl(
            'resource "aws_db_instance" "postgres" {\n'
            '  engine = "postgres"\n'
            '  engine_version = "16.3"\n'
            '}\n'
        )
        self.assertIn("do not hardcode", reason or "")

    def test_allows_only_the_requested_rds_engine_version(self) -> None:
        database = (
            'resource "aws_db_instance" "postgres" {\n'
            '  engine = "postgres"\n'
            '  engine_version = "16.3"\n'
            '}\n'
        )
        self.assertIsNone(rejected_hcl(database, message="Create PostgreSQL 16.3"))
        reason = rejected_hcl(database, message="Create PostgreSQL 15.4")
        self.assertIn("exactly 15.4", reason or "")

    def test_rejects_provider_version_other_than_the_default(self) -> None:
        reason = rejected_hcl(_versions("~> 5.0"), filename="versions.tf")
        self.assertIn("hashicorp/aws must use ~> 6.0", reason or "")
        self.assertIsNone(rejected_hcl(_versions("~> 6.0"), filename="versions.tf"))

    def test_requires_the_provider_version_named_by_the_user(self) -> None:
        message = "Use hashicorp/aws 5.81.0 for this stack."
        self.assertIsNone(rejected_hcl(_versions("5.81.0"), message=message, filename="versions.tf"))
        reason = rejected_hcl(_versions("~> 6.0"), message=message, filename="versions.tf")
        self.assertIn("must use 5.81.0", reason or "")

    def test_leaves_resource_files_alone(self) -> None:
        self.assertIsNone(
            rejected_hcl('resource "aws_s3_bucket" "data" {\n  bucket = "example"\n}\n')
        )

    def test_align_writes_the_default_or_the_requested_provider_version(self) -> None:
        self.assertIn(
            'version = "~> 6.0"',
            align_hcl(_versions("~> 5.0"), filename="versions.tf"),
        )
        requested = align_hcl(
            _versions("~> 6.0"),
            message="Use hashicorp/aws 5.81.0",
            filename="versions.tf",
        )
        self.assertIn('version = "5.81.0"', requested)
        self.assertNotIn("~> 6.0", requested)

    def test_align_keeps_a_previously_requested_version_on_a_later_edit(self) -> None:
        previous = _versions("5.81.0")
        updated = align_hcl(
            _versions("~> 6.0"),
            message="Add a private bucket",
            filename="versions.tf",
            previous=previous,
        )
        self.assertIn('version = "5.81.0"', updated)

    def test_align_strips_a_guessed_engine_version_and_keeps_a_requested_one(self) -> None:
        database = (
            'resource "aws_db_instance" "postgres" {\n'
            '  engine = "postgres"\n'
            '  engine_version = "16.3"\n'
            '}\n'
        )
        self.assertNotIn("engine_version", align_hcl(database))
        kept = align_hcl(database, message="Create PostgreSQL 15.4")
        self.assertIn('engine_version = "15.4"', kept)
        self.assertNotIn("16.3", kept)

    def test_apply_corrects_an_existing_versions_file(self) -> None:
        with TemporaryDirectory() as root:
            workspace = Path(root)
            (workspace / "versions.tf").write_text(_versions("~> 6.0"), encoding="utf-8")
            changed = apply_version_overrides(workspace, "Use aws provider version ~> 5.0")
            self.assertEqual(changed, ["versions.tf"])
            self.assertIn('version = "~> 5.0"', (workspace / "versions.tf").read_text(encoding="utf-8"))
            self.assertEqual(apply_version_overrides(workspace, "Add a subnet"), [])


def _versions(aws_version: str) -> str:
    return (
        "terraform {\n"
        "  required_providers {\n"
        "    aws = {\n"
        '      source  = "hashicorp/aws"\n'
        f'      version = "{aws_version}"\n'
        "    }\n"
        "  }\n"
        "}\n"
    )
