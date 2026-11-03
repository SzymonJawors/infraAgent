import unittest
from unittest.mock import patch

from app.services.github import GithubError, analyze_repository, terraform_files


class GithubAnalysisTests(unittest.TestCase):
    def test_detects_application_stack_and_terraform(self) -> None:
        tree = {
            "tree": [
                {"path": "README.md", "type": "blob"},
                {"path": "package.json", "type": "blob"},
                {"path": "Dockerfile", "type": "blob"},
                {"path": "infra/main.tf", "type": "blob"},
                {"path": "infra/modules/network/main.tf", "type": "blob"},
            ]
        }
        with patch("app.services.github._open", side_effect=[tree, {
            "content": "QXBwIHJ1bnMgb24gQVdT",  # "App runs on AWS"
        }]):
            result = analyze_repository("token", "owner/repo", "main")

        self.assertTrue(result["has_terraform"])
        self.assertEqual(result["terraform_file_count"], 2)
        self.assertEqual(result["terraform_files"], ["infra/main.tf", "infra/modules/network/main.tf"])
        self.assertEqual(result["technologies"], ["Docker", "Node.js", "AWS"])

    def test_recommends_infrastructure_when_terraform_is_missing(self) -> None:
        tree = {
            "tree": [
                {"path": "README.md", "type": "blob"},
                {"path": "pyproject.toml", "type": "blob"},
            ]
        }
        with patch("app.services.github._open", side_effect=[tree, {"content": ""}]):
            result = analyze_repository("token", "owner/repo", "main")

        self.assertFalse(result["has_terraform"])
        self.assertTrue(result["deployable_stack"])
        self.assertEqual(result["technologies"], ["Python"])
        self.assertIn("No Terraform", result["recommendation"])
        self.assertIn("Propose AWS Terraform", result["suggested_prompt"])

    def test_marks_repository_without_stack_as_not_deployable(self) -> None:
        tree = {"tree": [{"path": "solve.py", "type": "blob"}]}
        with patch("app.services.github._open", return_value=tree):
            result = analyze_repository("token", "owner/repo", "main")

        self.assertFalse(result["deployable_stack"])
        self.assertIn("not recommended", result["recommendation"])
        self.assertIn("Do not generate Terraform", result["suggested_prompt"])

    def test_rejects_invalid_repository_names(self) -> None:
        with self.assertRaises(GithubError):
            analyze_repository("token", "../repo", "main")

    def test_imports_nested_terraform_files(self) -> None:
        tree = {"tree": [{"path": "infra/main.tf", "type": "blob"}]}
        with patch("app.services.github._open", side_effect=[
            tree,
            {"content": "cmVzb3VyY2UgImF3c19zMyIgImJ1Y2tldCIge30="},
        ]):
            files = terraform_files("token", "owner/repo", "main")

        self.assertEqual(files, {"infra/main.tf": 'resource "aws_s3" "bucket" {}'})


if __name__ == "__main__":
    unittest.main()
