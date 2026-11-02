"""Run the local InfraAgent evaluations.

The model key is used. AWS credentials are removed for the run, and apply is
never called. Each case gets a fresh temporary directory.
"""

import os
import tempfile
from pathlib import Path

from app.core.config import get_settings
from app.services.agent import InfraAgent
from app.services.scanner import WorkspaceScanner
from evals.braintrust_export import experiment_row, publish
from evals.score import CASES, score_case

_AWS_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
)


def main() -> int:
    settings = get_settings().model_copy(
        update={"aws_access_key_id": "", "aws_secret_access_key": ""}
    )
    if not settings.openai_api_key:
        print("missing_model_key")
        return 2

    saved = {name: os.environ.pop(name) for name in _AWS_ENV if name in os.environ}
    failed = 0
    rows: list[dict] = []
    try:
        for case in CASES:
            workspace = Path(tempfile.mkdtemp(prefix=f"infraagent-{case.name}-"))
            turn = InfraAgent(workspace, settings).run(case.message)
            files_text = WorkspaceScanner(workspace).read()
            scores = score_case(
                case,
                reply=turn.reply,
                files_text=files_text,
                validation_ok=turn.validation.ok,
                scan_output=turn.scan.output(),
                scan_ok=turn.scan.ok,
                plan_output=turn.plan.output(),
            )
            ok = all(scores.values())
            failed += not ok
            rows.append(
                experiment_row(
                    case.message,
                    turn.files,
                    turn.validation.ok,
                    turn.scan.ok,
                    scores,
                )
            )
            print(f"{case.name} {'pass' if ok else 'fail'}")
            for name, passed in scores.items():
                print(f"  {name} {passed}")
            print(f"  files {','.join(turn.files) or '-'}")
            print(f"  validation {turn.validation.ok}")
            print(f"  scan {turn.scan.ok}")
        _publish(rows, settings.openai_model, settings.braintrust_api_key)
    finally:
        os.environ.update(saved)
    print(f"result {'pass' if failed == 0 else 'fail'}")
    return 1 if failed else 0


def _publish(rows: list[dict], model: str, api_key: str) -> None:
    try:
        if publish(rows, model, api_key):
            print("braintrust sent")
    except Exception as exc:
        text = str(exc)
        if api_key:
            text = text.replace(api_key, "[redacted]")
        print("braintrust_send_failed", " ".join(text.split())[:200])


if __name__ == "__main__":
    raise SystemExit(main())
