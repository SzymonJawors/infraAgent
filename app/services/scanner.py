from pathlib import Path


class WorkspaceScanner:
    """Reads existing ``.tf`` files in ``workspace/`` for agent context.

    Later it may read Terraform state. It does not write files or call AWS.
    """

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
