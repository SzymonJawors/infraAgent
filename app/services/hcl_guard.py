import re

_BLOCKED = re.compile(
    r"local-exec|remote-exec|provisioner|data\s+[\"']external[\"']",
    re.IGNORECASE,
)
_HARDCODED_AMI = re.compile(r"(?m)^\s*ami\s*=\s*['\"]ami-[a-z0-9]+['\"]\s*$", re.IGNORECASE)


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
    if _HARDCODED_AMI.search(content):
        return (
            "Rejected: do not hardcode an AMI id. Define an aws_ami data source "
            "with Amazon owner, available state, architecture, and region-safe "
            "name filters, then use data.aws_ami.<name>.id."
        )
    return None
