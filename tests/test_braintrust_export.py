import json
import unittest
from unittest.mock import MagicMock, patch

from evals.braintrust_export import experiment_row, publish


class BraintrustExportTests(unittest.TestCase):
    def test_row_keeps_scores_and_drops_a_secret(self) -> None:
        secret = "sk-live-secret-value"
        row = experiment_row(
            message="Private bucket in eu-central-1",
            files=["main.tf"],
            validation_ok=True,
            scan_ok=False,
            scores={"no_secrets": True, "not_public": False},
        )
        blob = json.dumps(row)
        self.assertNotIn(secret, blob)
        self.assertNotIn("aws_secret_access_key", blob)
        self.assertEqual(row["input"], "Private bucket in eu-central-1")
        self.assertEqual(row["output"]["files"], ["main.tf"])
        self.assertEqual(row["output"]["validation_ok"], True)
        self.assertEqual(row["output"]["scan_ok"], False)
        self.assertEqual(row["scores"], {"no_secrets": 1, "not_public": 0})

    def test_empty_key_does_not_open_an_experiment(self) -> None:
        with patch("braintrust.init") as init:
            sent = publish([], "gpt-5.4-mini", "  ")
        self.assertFalse(sent)
        init.assert_not_called()

    def test_key_logs_scores_without_the_key(self) -> None:
        secret = "bt-secret-value"
        row = experiment_row("make a bucket", ["main.tf"], True, True, {"validated": True})
        experiment = MagicMock()
        with patch("braintrust.init", return_value=experiment) as init:
            sent = publish([row], "gpt-5.4-mini", secret)
        self.assertTrue(sent)
        init.assert_called_once_with(
            project="InfraAgent",
            experiment="gpt-5.4-mini",
            api_key=secret,
        )
        experiment.log.assert_called_once_with(
            input=row["input"],
            output=row["output"],
            scores=row["scores"],
        )
        logged = json.dumps(experiment.log.call_args.kwargs)
        self.assertNotIn(secret, logged)
