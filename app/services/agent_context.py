import difflib
import json
import re
from pathlib import Path


_AWS_REGIONS = (
    "af-south-1", "ap-east-1", "ap-northeast-1", "ap-northeast-2", "ap-northeast-3",
    "ap-south-1", "ap-south-2", "ap-southeast-1", "ap-southeast-2", "ap-southeast-3",
    "ap-southeast-4", "ca-central-1", "ca-west-1", "eu-central-1", "eu-central-2",
    "eu-north-1", "eu-south-1", "eu-south-2", "eu-west-1", "eu-west-2", "eu-west-3",
    "il-central-1", "me-central-1", "me-south-1", "mx-central-1", "sa-east-1",
    "us-east-1", "us-east-2", "us-gov-east-1", "us-gov-west-1", "us-west-1", "us-west-2",
)


def read_conversation(path: Path) -> list[dict[str, str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    return [
        item for item in payload[-12:]
        if isinstance(item, dict)
        and item.get("role") in {"user", "assistant"}
        and isinstance(item.get("content"), str)
    ]


def write_conversation(path: Path, messages: list[dict[str, str]]) -> None:
    try:
        path.write_text(
            json.dumps(messages[-12:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        return


def detect_region(message: str) -> str | None:
    normalized = re.sub(r"\s+", " ", message.lower()).strip()
    aliases = {
        "frankfurt": "eu-central-1",
        "ireland": "eu-west-1",
        "london": "eu-west-2",
        "stockholm": "eu-north-1",
        "virginia": "us-east-1",
        "ohio": "us-east-2",
        "oregon": "us-west-2",
        "tokyo": "ap-northeast-1",
        "singapore": "ap-southeast-1",
        "sydney": "ap-southeast-2",
    }
    for alias, region in aliases.items():
        if re.search(rf"\b{alias}\b", normalized):
            return region
    if re.search(r"\beu[\s-]+central(?:[\s-]+1)?\b", normalized):
        return "eu-central-1"
    direct = re.search(r"\b([a-z]{2}-[a-z]+-\d)\b", normalized)
    if direct and direct.group(1) in _AWS_REGIONS:
        return direct.group(1)
    for match in re.finditer(r"\b([a-z]{2})[\s-]+([a-z]+(?:[\s-]+[a-z]+)*)[\s-]*(\d)\b", normalized):
        compact = "".join(match.groups()).replace(" ", "")
        closest = difflib.get_close_matches(
            compact,
            [region.replace("-", "") for region in _AWS_REGIONS],
            n=1,
            cutoff=0.82,
        )
        if closest:
            return next(region for region in _AWS_REGIONS if region.replace("-", "") == closest[0])
    return None


def provider_region(terraform: str) -> str | None:
    match = re.search(
        r'provider\s+"aws"\s*\{(?:(?!\}).)*?\bregion\s*=\s*"([^"]+)"',
        terraform,
        re.IGNORECASE | re.DOTALL,
    )
    return match.group(1).strip() if match else None


def update_provider_region(workspace: Path, region: str) -> str:
    provider_path = workspace / "provider.tf"
    if not provider_path.is_file():
        return "No provider.tf exists yet; create it with the confirmed region."
    content = provider_path.read_text(encoding="utf-8")
    updated, replacements = re.subn(
        r'(?m)^(\s*region\s*=\s*)("[^"]*"|[^\s#]+)',
        rf'\g<1>"{region}"',
        content,
        count=1,
    )
    if replacements == 0:
        return "provider.tf has no region assignment; add the confirmed region to its AWS provider block."
    if updated != content:
        provider_path.write_text(updated, encoding="utf-8")
        return f"Applied the region amendment directly to provider.tf: {region}."
    return f"provider.tf already uses the confirmed region: {region}."
