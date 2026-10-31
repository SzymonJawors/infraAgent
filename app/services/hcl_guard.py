import re

_BLOCKED = re.compile(
    r"local-exec|remote-exec|provisioner|data\s+[\"']external[\"']",
    re.IGNORECASE,
)


def rejected_hcl(content: str) -> str | None:
    """Return a rejection reason when the file could run a program during plan."""
    lowered = content.lower()
    if "aws_secret_access_key" in lowered or "aws_access_key_id" in lowered:
        return "Rejected: do not put AWS credentials in Terraform files."
    if _BLOCKED.search(content):
        return (
            "Rejected: local-exec, remote-exec, provisioner, and external data "
            "sources are not allowed."
        )
    return None
