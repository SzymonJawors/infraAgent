import unittest

from app.services.error_analysis import analyze_terraform_error


class TerraformErrorAnalysisTests(unittest.TestCase):
    def test_explains_existing_iam_role(self) -> None:
        result = analyze_terraform_error(
            "Error: creating IAM Role (server-ec2-role): EntityAlreadyExists: Role with name server-ec2-role already exists.",
            "terraform apply -auto-approve",
        )
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.category, "already_exists")
        self.assertEqual(result.resource, "server-ec2-role")
        self.assertIn("import", result.recommendation)

    def test_explains_access_denied(self) -> None:
        result = analyze_terraform_error("AccessDenied: not authorized to perform iam:CreateRole", "terraform apply")
        self.assertEqual(result.category, "permissions")

    def test_has_generic_terraform_fallback(self) -> None:
        result = analyze_terraform_error("provider returned an unknown error", "terraform destroy")
        self.assertEqual(result.category, "terraform")


if __name__ == "__main__":
    unittest.main()
