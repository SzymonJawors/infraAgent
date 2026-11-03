import unittest

from app.services.reviewer import _review_report
from app.services.terraform import CommandResult


def _result(ok: bool, output: str = "", *, missing_binary: bool = False) -> CommandResult:
    return CommandResult(
        ok=ok,
        command="terraform",
        stdout=output,
        stderr="",
        returncode=0 if ok else 1,
        missing_binary=missing_binary,
    )


class ReviewReportTests(unittest.TestCase):
    def test_validation_failure_is_structured_and_retryable(self) -> None:
        report = _review_report(
            _result(False, "Error: Invalid block"),
            _result(False, "Skipped"),
            _result(True),
            None,
        )

        self.assertEqual(report.stage, "terraform_validate")
        self.assertEqual(report.category, "configuration_error")
        self.assertTrue(report.retryable)
        self.assertIn('"suggested_action"', report.as_tool_result())

    def test_missing_credentials_are_non_retryable(self) -> None:
        report = _review_report(
            _result(True),
            _result(False, "AWS credentials are not configured"),
            _result(True),
            "AWS credentials are missing",
        )

        self.assertEqual(report.category, "credentials_missing")
        self.assertFalse(report.retryable)

    def test_success_is_structured(self) -> None:
        report = _review_report(_result(True), _result(True), _result(True), None)

        self.assertTrue(report.ok)
        self.assertEqual(report.category, "passed")
        self.assertFalse(report.retryable)


if __name__ == "__main__":
    unittest.main()
