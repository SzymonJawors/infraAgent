import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.config import Settings
from app.services.terraform import CLI_CONFIG_PATH, PLUGIN_CACHE_DIR, TerraformService


class TerraformEnvironmentTests(unittest.TestCase):
    def test_uses_shared_project_plugin_cache_for_all_sessions(self) -> None:
        with TemporaryDirectory() as root:
            session = Path(root) / "session-id"
            session.mkdir()

            environment = TerraformService(session, Settings())._env()

            self.assertEqual(environment["TF_CLI_CONFIG_FILE"], str(CLI_CONFIG_PATH))
            self.assertTrue(PLUGIN_CACHE_DIR.is_dir())
            config = CLI_CONFIG_PATH.read_text(encoding="utf-8")
            self.assertIn("filesystem_mirror", config)
            self.assertIn(PLUGIN_CACHE_DIR.as_posix(), config)

    def test_destroy_runs_after_termination_protection_is_removed(self) -> None:
        service = TerraformService(Path("C:/tmp/session"), Settings(aws_region="eu-central-1"))
        with patch.object(service, "_disable_termination_protection", return_value=None):
            with patch.object(service, "_run") as run:
                run.return_value.ok = True
                service.destroy()
        run.assert_called_once()
        self.assertIn("destroy", run.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
