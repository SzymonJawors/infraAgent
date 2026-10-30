from pathlib import Path


class WorkspaceScanner:
    """Reads ``.tf`` files already stored for a session. It does not write."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def files(self) -> list[str]:
        return sorted(path.name for path in self.workspace.glob("*.tf") if path.is_file())

    def read(self) -> str:
        chunks: list[str] = []
        for name in self.files():
            text = (self.workspace / name).read_text(encoding="utf-8")
            chunks.append(f"----- {name} -----\n{text}")
        return "\n\n".join(chunks)
