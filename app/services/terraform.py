from pathlib import Path


class TerraformService:
    """The only module allowed to invoke the Terraform CLI.

    Working directory is ``workspace/``. Later this wraps ``init``, ``fmt``,
    ``validate``, ``plan``, and — only after an explicit user confirmation —
    ``apply`` and ``destroy``. Secrets stay in the process environment and
    never get written into ``.tf`` files.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
