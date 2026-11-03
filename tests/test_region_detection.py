import tempfile
import unittest
from pathlib import Path

from app.services.agent import _detect_region, _finalize_reply, _is_fast_request, _provider_region, _update_provider_region


class RegionDetectionTests(unittest.TestCase):
    def test_server_and_region_follow_up_are_not_fast_path_hacks(self) -> None:
        self.assertTrue(_is_fast_request("zrob server", []))
        self.assertFalse(_is_fast_request("frankfurt", []))
        self.assertFalse(_is_fast_request("eu central 1", []))

    def test_detects_standard_region(self) -> None:
        self.assertEqual(_detect_region("Create it in eu-central-1."), "eu-central-1")

    def test_detects_region_aliases(self) -> None:
        self.assertEqual(_detect_region("Use eu central."), "eu-central-1")
        self.assertEqual(_detect_region("Deploy in Frankfurt."), "eu-central-1")
        self.assertEqual(_detect_region("eu cenntral 1"), "eu-central-1")
        self.assertEqual(_detect_region("ap south east 2"), "ap-southeast-2")
        self.assertEqual(_detect_region("use us east 1"), "us-east-1")

    def test_returns_none_when_region_is_missing(self) -> None:
        self.assertIsNone(_detect_region("Create a private bucket."))

    def test_agent_reply_does_not_offer_optional_follow_up(self) -> None:
        reply = "The server is ready.\n\nIf you want, I can make it private."

        self.assertEqual(_finalize_reply(reply), "The server is ready.")

    def test_agent_reply_collapses_excessive_spacing(self) -> None:
        reply = "What changed\n\n\n\n- Added a VPC  \n\n\nWhy\n\n- It isolates the application."

        self.assertEqual(
            _finalize_reply(reply),
            "What changed\n\n- Added a VPC\n\nWhy\n\n- It isolates the application.",
        )

    def test_updates_existing_provider_region(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            provider = workspace / "provider.tf"
            provider.write_text('provider "aws" {\n  region = "eu-central-1"\n}\n', encoding="utf-8")

            message = _update_provider_region(workspace, "us-east-1")

            self.assertIn("us-east-1", message)
            self.assertIn('region = "us-east-1"', provider.read_text(encoding="utf-8"))

    def test_reads_region_from_existing_provider(self) -> None:
        terraform = 'provider "aws" {\n  region = "eu-central-1"\n}\n'
        self.assertEqual(_provider_region(terraform), "eu-central-1")


if __name__ == "__main__":
    unittest.main()
