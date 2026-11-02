"""Send local evaluation scores to Braintrust.

The payload is the request, file names, and pass/fail flags. Credentials and
Terraform source stay out of it.
"""


def experiment_row(
    message: str,
    files: list[str],
    validation_ok: bool,
    scan_ok: bool,
    scores: dict[str, bool],
) -> dict:
    return {
        "input": message,
        "output": {
            "files": list(files),
            "validation_ok": validation_ok,
            "scan_ok": scan_ok,
        },
        "scores": {name: 1 if passed else 0 for name, passed in scores.items()},
    }


def publish(rows: list[dict], model: str, api_key: str) -> bool:
    """Log one experiment. An empty key leaves the run on the console."""
    key = api_key.strip()
    if not key:
        return False
    import braintrust

    experiment = braintrust.init(project="InfraAgent", experiment=model, api_key=key)
    for row in rows:
        experiment.log(
            input=row["input"],
            output=row["output"],
            scores=row["scores"],
        )
    summary = getattr(experiment, "summarize", None)
    if summary is not None:
        summary()
    else:
        experiment.flush()
    return True
