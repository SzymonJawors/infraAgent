import json
import io
import shutil
import unittest
import zipfile
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import app
from app.api import routes
from app.api.routes import _command_body, _plan_summary
from app.services.agent import AgentTurn
from app.services.agent_context import write_conversation
from app.services.approval import files_digest, matching_receipt, read_receipt, write_receipt
from app.services.sessions import delete_session, list_sessions, start_operation, touch_session
from app.services.sessions import WORKSPACE_ROOT
from app.services.terraform import CommandResult
from app.services.workspace_lock import WorkspaceBusyError

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

    def test_touching_existing_session_without_title_preserves_name(self) -> None:
        touch_session(self.session, "Photo database")
        touch_session(self.session)

        session = next(item for item in list_sessions() if item["id"] == self.session)
        self.assertEqual(session["title"], "Photo database")

    def test_create_session_persists_before_rename(self) -> None:
        response = self.client.post(f"/sessions/{self.session}")

        self.assertEqual(response.status_code, 201)
        renamed = self.client.patch(
            f"/sessions/{self.session}",
            json={"title": "Production environment"},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "Production environment")

    def test_conversation_endpoint_reads_persisted_agent_history(self) -> None:
        self.workspace.mkdir(parents=True)
        write_conversation(
            self.workspace / ".agent-conversation.json",
            [
                {"role": "user", "content": "Create a VPC"},
                {"role": "assistant", "content": "Created the VPC configuration."},
            ],
        )

        response = self.client.get(f"/sessions/{self.session}/conversation")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["messages"], [
            {"role": "user", "content": "Create a VPC"},
            {"role": "assistant", "content": "Created the VPC configuration."},
        ])

    def test_export_contains_terraform_without_workspace_artifacts(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "main.tf").write_text(
            'resource "aws_s3_bucket" "private" {}',
            encoding="utf-8",
        )
        (self.workspace / "terraform.tfstate").write_text("sensitive state", encoding="utf-8")
        (self.workspace / ".agent-job.json").write_text("internal state", encoding="utf-8")

        response = self.client.get(f"/sessions/{self.session}/export")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"main.tf", "README.md"},
            )
            self.assertIn("aws_s3_bucket", archive.read("main.tf").decode())

    def test_message_does_not_replace_custom_session_title(self) -> None:
        touch_session(self.session, "Production environment")
        turn = AgentTurn(
            reply="ok",
            files=[],
            validation=_result(True),
            plan=_result(False),
            scan=_result(False),
            registry_docs=False,
        )
        with patch("app.api.routes.InfraAgent") as agent:
            agent.return_value.run.return_value = turn
            response = self.client.post(
                f"/sessions/{self.session}/messages",
                json={"message": "Create a VPC in Frankfurt"},
            )
        self.assertEqual(response.status_code, 200)
        session = next(item for item in list_sessions() if item["id"] == self.session)
        self.assertEqual(session["title"], "Production environment")

    def test_missing_region_is_clarified_without_starting_agent(self) -> None:
        touch_session(self.session)
        with patch("app.api.routes.InfraAgent") as agent:
            response = self.client.post(
                f"/sessions/{self.session}/messages",
                json={"message": "I need a database for images"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("Which AWS region", response.json()["reply"])
        agent.assert_not_called()

    def test_message_returns_conflict_when_workspace_lease_is_busy(self) -> None:
        touch_session(self.session)
        with patch(
            "app.api.routes.workspace_lease",
            side_effect=WorkspaceBusyError,
        ):
            response = self.client.post(
                f"/sessions/{self.session}/messages",
                json={"message": "Create a VPC in Frankfurt"},
            )
        self.assertEqual(response.status_code, 409)

    def tearDown(self) -> None:
        self.settings.stop()
        delete_session(self.session)

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

    def test_receipt_stores_plan_artifact_metadata(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "main.tf").write_text("a\n", encoding="utf-8")

        write_receipt(
            self.workspace,
            "apply",
            plan_output="Plan: 1 to add, 0 to change, 0 to destroy.",
            session_id=self.session,
        )
        receipt = read_receipt(self.workspace, "apply")

        self.assertIsNotNone(receipt)
        self.assertEqual(receipt["session_id"], self.session)
        self.assertIn("Plan: 1 to add", receipt["plan_output"])

    def test_expired_receipt_is_removed(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "main.tf").write_text("a\n", encoding="utf-8")
        write_receipt(self.workspace, "apply")
        path = self.workspace / ".infraagent" / "apply.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["created_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(payload), encoding="utf-8")

        self.assertFalse(matching_receipt(self.workspace, "apply"))
        self.assertFalse(path.exists())

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

    def test_cancel_operation_keeps_cancelled_status(self) -> None:
        touch_session(self.session)
        operation_id = start_operation(self.session, "apply")

        class FakeService:
            def __init__(self) -> None:
                self.cancelled = False

            def cancel(self) -> None:
                self.cancelled = True

        service = FakeService()
        routes._ACTIVE_SERVICES[(self.session, operation_id)] = service
        try:
            response = self.client.post(
                f"/sessions/{self.session}/operations/{operation_id}/cancel",
            )
        finally:
            routes._ACTIVE_SERVICES.pop((self.session, operation_id), None)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "cancelled")
        self.assertEqual(response.json()["summary"], "Cancellation requested.")
        self.assertTrue(service.cancelled)

    def test_invalid_session(self) -> None:
        response = self.client.post("/sessions/a.b/apply", json={"approve": True})
        self.assertEqual(response.status_code, 400)

    def test_session_can_be_renamed_without_changing_workspace_id(self) -> None:
        self.workspace.mkdir(parents=True)
        touch_session(self.session, "Untitled environment")
        response = self.client.patch(
            f"/sessions/{self.session}",
            json={"title": "Production Frankfurt"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Production Frankfurt")
        self.assertTrue(self.workspace.is_dir())
        self.assertEqual(
            next(item["title"] for item in self.client.get("/sessions").json()["sessions"] if item["id"] == self.session),
            "Production Frankfurt",
        )

    def test_session_status_reads_managed_resources_from_state(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "terraform.tfstate").write_text(
            '{"version": 4, "resources": [{"mode": "managed", "type": "aws_instance", "instances": [{}]}]}',
            encoding="utf-8",
        )
        response = self.client.get(f"/sessions/{self.session}/status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "applied")
        self.assertEqual(response.json()["managed_resources"], 1)

    def test_agent_status_is_restored_from_session_workspace(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / ".agent-job.json").write_text(
            '{"status":"running","progress":[{"stage":"generating","message":"Working"}]}',
            encoding="utf-8",
        )
        response = self.client.get(f"/sessions/{self.session}/agent-status")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "running")
        self.assertEqual(response.json()["progress"][0]["stage"], "generating")

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
                json={"message": "leave it in eu-central-1"},
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
                json={"message": "leave it in eu-central-1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertFalse((self.workspace / ".infraagent" / "apply.json").exists())
        self.assertFalse(response.json()["scan"]["ok"])

    def test_message_stream_returns_status_and_result_events(self) -> None:
        self.workspace.mkdir(parents=True)
        turn = AgentTurn(
            reply="streamed",
            files=["main.tf"],
            validation=_result(True),
            plan=_result(True),
            scan=_result(True),
            registry_docs=True,
        )
        with patch("app.api.routes.InfraAgent") as agent:
            def run_with_progress(message, on_progress=None):
                if on_progress:
                    on_progress("generating", "Generating Terraform configuration...", "AI agent / Terraform tools")
                return turn

            agent.return_value.run.side_effect = run_with_progress
            response = self.client.post(
                f"/sessions/{self.session}/messages/stream",
                json={"message": "create it in eu-central-1"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("event: status", response.text)
        self.assertIn('"stage": "generating"', response.text)
        self.assertIn("event: result", response.text)
        self.assertIn('"reply": "streamed"', response.text)
        self.assertIn("text/event-stream", response.headers["content-type"])

    def test_delete_session_removes_workspace(self) -> None:
        self.workspace.mkdir(parents=True)
        (self.workspace / "main.tf").write_text("resource \"aws_s3_bucket\" \"test\" {}", encoding="utf-8")
        response = self.client.delete(f"/sessions/{self.session}")

        self.assertEqual(response.status_code, 204)
        self.assertFalse(self.workspace.exists())

    def test_empty_destroy_plan_does_not_create_receipt(self) -> None:
        self.workspace.mkdir(parents=True)
        result = CommandResult(
            ok=True,
            command="terraform plan -destroy",
            stdout="No changes. No objects need to be destroyed.\n",
            stderr="",
            returncode=0,
        )
        with patch("app.api.routes.TerraformService") as service:
            service.return_value.plan_destroy.return_value = result
            response = self.client.post(f"/sessions/{self.session}/plans/destroy")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_managed_resources")
        self.assertFalse(matching_receipt(self.workspace, "destroy"))

    def test_plan_summary_extracts_resources_and_cost_warnings(self) -> None:
        summary = _plan_summary(
            """
            # aws_instance.server will be created
            # aws_db_instance.main will be created
            Plan: 2 to add, 1 to change, 0 to destroy.
            multi_az = true
            """
        )

        self.assertEqual((summary.to_add, summary.to_change, summary.to_destroy), (2, 1, 0))
        self.assertEqual(summary.resources, ["aws_db_instance.main", "aws_instance.server"])
        self.assertEqual(
            [(item.address, item.action) for item in summary.resource_actions],
            [
                ("aws_db_instance.main", "create"),
                ("aws_instance.server", "create"),
            ],
        )
        self.assertTrue(summary.cost_warnings)

    def test_plan_summary_warns_for_s3_and_kms_costs(self) -> None:
        summary = _plan_summary(
            """
            # aws_s3_bucket.private will be created
            # aws_kms_key.s3 will be created
            Plan: 2 to add, 0 to change, 0 to destroy.
            """
        )

        self.assertTrue(any("S3" in warning for warning in summary.cost_warnings))
        self.assertTrue(any("KMS" in warning for warning in summary.cost_warnings))

    def test_zero_destroy_plan_does_not_create_receipt(self) -> None:
        self.workspace.mkdir(parents=True)
        result = CommandResult(
            ok=True,
            command="terraform plan -destroy",
            stdout="Plan: 0 to add, 0 to change, 0 to destroy.\n",
            stderr="",
            returncode=0,
        )
        with patch("app.api.routes.TerraformService") as service:
            service.return_value.plan_destroy.return_value = result
            response = self.client.post(f"/sessions/{self.session}/plans/destroy")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "no_managed_resources")
        self.assertFalse(matching_receipt(self.workspace, "destroy"))

    def test_destroy_summary_uses_explicit_resources_when_counts_are_stale(self) -> None:
        summary = _plan_summary(
            """
            # aws_instance.server will be destroyed
            # aws_security_group.server will be destroyed
            Plan: 0 to add, 0 to change, 0 to destroy.
            """,
            destroy=True,
        )

        self.assertEqual(summary.to_destroy, 2)
        self.assertEqual(
            summary.resources,
            ["aws_instance.server", "aws_security_group.server"],
        )

    def test_destroy_command_returns_summary_for_completed_destroy(self) -> None:
        result = CommandResult(
            ok=True,
            command="terraform destroy -input=false -auto-approve",
            stdout=(
                "# aws_instance.server will be destroyed\n"
                "Plan: 0 to add, 0 to change, 1 to destroy.\n"
            ),
            stderr="",
            returncode=0,
        )

        body = _command_body(result)

        self.assertEqual(body.status, "completed")
        self.assertIsNotNone(body.plan_summary)
        self.assertEqual(body.plan_summary.to_destroy, 1)
        self.assertEqual(body.plan_summary.resources, ["aws_instance.server"])


if __name__ == "__main__":
    unittest.main()
