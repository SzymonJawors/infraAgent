import shutil
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.services.agent import AgentTurn
from app.services.approval import files_digest, matching_receipt, write_receipt
from app.services.sessions import WORKSPACE_ROOT
from app.services.terraform import CommandResult

_FAKE = Settings(
    openai_api_key="sk-test",
    aws_access_key_id="AKIAFAKEKEY",
    aws_secret_access_key="fake-secret",
    aws_region="eu-central-1",
    openai_model="gpt-5.4-mini",
)


def _result(ok: bool) -> CommandResult:
    return CommandResult(ok=ok, command="terraform", stdout="", stderr="", returncode=0 if ok else 1)


class ApprovalApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = "approvaltest"
        self.workspace = WORKSPACE_ROOT / self.session
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.client = TestClient(app)
        self.settings = patch("app.api.routes.get_settings", return_value=_FAKE)
        self.settings.start()

    def tearDown(self) -> None:
        self.settings.stop()
        if self.workspace.exists():
            shutil.rmtree(self.workspace)

    def test_digest_changes_when_file_changes(self) -> None:
        self.workspace.mkdir(parents=True)
        file = self.workspace / "main.tf"
        file.write_text("a\n", encoding="utf-8")
        first = files_digest(self.workspace)
        write_receipt(self.workspace, "apply")
        self.assertTrue(matching_receipt(self.workspace, "apply"))
        file.write_text("b\n", encoding="utf-8")
        self.assertNotEqual(first, files_digest(self.workspace))
        self.assertFalse(matching_receipt(self.workspace, "apply"))

    def test_apply_without_receipt_does_not_call_terraform(self) -> None:
        with patch("app.api.routes.TerraformService") as service:
            response = self.client.post(
                f"/sessions/{self.session}/apply",
                json={"approve": True},
            )
        self.assertEqual(response.status_code, 409)
        service.assert_not_called()

    def test_false_approval_does_not_call_terraform(self) -> None:
        with patch("app.api.routes.TerraformService") as service:
            response = self.client.post(
                f"/sessions/{self.session}/apply",
                json={"approve": False},
            )
        self.assertEqual(response.status_code, 400)
        service.assert_not_called()

    def test_changed_files_do_not_apply(self) -> None:
        self.workspace.mkdir(parents=True)
        file = self.workspace / "main.tf"
        file.write_text("a\n", encoding="utf-8")
        write_receipt(self.workspace, "apply")
        file.write_text("b\n", encoding="utf-8")
        with patch("app.api.routes.TerraformService") as service:
            response = self.client.post(
                f"/sessions/{self.session}/apply",
                json={"approve": True},
            )
        self.assertEqual(response.status_code, 409)
        service.assert_not_called()

    def test_apply_receipt_does_not_destroy(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "main.tf").write_text("a\n", encoding="utf-8")
        write_receipt(self.workspace, "apply")
        with patch("app.api.routes.TerraformService") as service:
            response = self.client.post(
                f"/sessions/{self.session}/destroy",
                json={"approve": True},
            )
        self.assertEqual(response.status_code, 409)
        service.assert_not_called()

    def test_missing_aws_keys_do_not_call_terraform(self) -> None:
        empty = Settings(
            openai_api_key="sk-test",
            aws_access_key_id="",
            aws_secret_access_key="",
        )
        with patch("app.api.routes.get_settings", return_value=empty):
            with patch("app.api.routes.TerraformService") as service:
                response = self.client.post(
                    f"/sessions/{self.session}/apply",
                    json={"approve": True},
                )
        self.assertEqual(response.status_code, 503)
        service.assert_not_called()

    def test_invalid_session(self) -> None:
        response = self.client.post("/sessions/a.b/apply", json={"approve": True})
        self.assertEqual(response.status_code, 400)

    def test_new_message_clears_receipt(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "main.tf").write_text("a\n", encoding="utf-8")
        write_receipt(self.workspace, "apply")
        turn = AgentTurn(
            reply="ok",
            files=["main.tf"],
            validation=_result(True),
            plan=_result(False),
            scan=_result(False),
            registry_docs=False,
        )
        with patch("app.api.routes.InfraAgent") as agent:
            agent.return_value.run.return_value = turn
            response = self.client.post(
                f"/sessions/{self.session}/messages",
                json={"message": "leave it"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.workspace / ".infraagent" / "apply.json").exists())

    def test_failed_scan_does_not_store_apply_receipt(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "main.tf").write_text("a\n", encoding="utf-8")
        turn = AgentTurn(
            reply="ok",
            files=["main.tf"],
            validation=_result(True),
            plan=_result(True),
            scan=_result(False),
            registry_docs=False,
        )
        with patch("app.api.routes.InfraAgent") as agent:
            agent.return_value.run.return_value = turn
            response = self.client.post(
                f"/sessions/{self.session}/messages",
                json={"message": "leave it"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.workspace / ".infraagent" / "apply.json").exists())
        self.assertFalse(response.json()["scan"]["ok"])


if __name__ == "__main__":
    unittest.main()
