import re
from pathlib import Path

_BLOCKED = re.compile(
    r"local-exec|remote-exec|provisioner|data\s+[\"']external[\"']",
    re.IGNORECASE,
)
_HARDCODED_AMI = re.compile(r"(?m)^\s*ami\s*=\s*['\"]ami-[a-z0-9]+['\"]\s*$", re.IGNORECASE)
_RDS_ENGINE_VALUE = re.compile(
    r'resource\s+"aws_db_instance"\s+"[^"]+"\s*\{(?P<body>(?:(?!\n\s*\}).)*?)(?:\n\s*\}|$)',
    re.IGNORECASE | re.DOTALL,
)
_ENGINE_ASSIGNMENT = re.compile(r'(?m)^\s*engine_version\s*=\s*"([^"]+)"')
_ENGINE_REQUEST = re.compile(
    r"(?:engine[_\s-]*version|postgres(?:ql)?|silnik(?:a)?)"
    r"(?:\s+(?:wersj[aąeę]|version))?"
    r"\s*[:=]?\s*"
    r"(\d+(?:\.\d+){1,2})",
    re.IGNORECASE,
)
_PROVIDER_REQUEST = re.compile(
    r"(?:(?:wersj[aąeę]|version|provider)\s+){0,2}"
    r"(?:hashicorp/)?"
    r"(?P<name>aws|random|archive)\b"
    r"(?:\s+(?:provider|wersj[aąeę]|version)){0,2}"
    r"\s+"
    r"(?P<op>~>\s*|>=\s*|=\s*)?"
    r"(?P<ver>\d+\.\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_PROVIDER_BLOCK = re.compile(
    r"(?P<block>[A-Za-z0-9_]+)\s*=\s*\{(?P<body>[^{}]*)\}",
    re.DOTALL,
)
_DEFAULT_PROVIDER_VERSIONS = {"aws": "~> 6.0", "random": "~> 3.0"}


def requested_provider_constraints(message: str) -> dict[str, str]:
    """Provider constraints explicitly named in the user message."""
    found: dict[str, str] = {}
    for match in _PROVIDER_REQUEST.finditer(message):
        name = match.group("name").lower()
        operator = re.sub(r"\s+", "", match.group("op") or "=")
        version = match.group("ver")
        found[name] = version if operator == "=" else f"{operator} {version}"
    return found


def requested_engine_version(message: str) -> str | None:
    """RDS engine version explicitly named in the user message."""
    match = _ENGINE_REQUEST.search(message)
    return match.group(1) if match else None


def align_hcl(content: str, message: str = "", filename: str = "", previous: str = "") -> str:
    """Rewrite provider and RDS engine versions to the constraint that should ship.

    A version named in this message wins. Otherwise an existing version stays, so a
    later unrelated edit does not reset it. A new provider block falls back to the
    default aws and random constraints.
    """
    requested = requested_provider_constraints(message)
    updated = _rewrite_provider_blocks(content, requested, _declared_providers(previous))
    if filename == "versions.tf":
        updated = _insert_missing_providers(updated, message)
    return _align_engine_version(updated, message, previous)


def apply_version_overrides(workspace: Path, message: str) -> list[str]:
    """Correct provider and engine versions already stored in a session."""
    changed: list[str] = []
    files = sorted(path for path in workspace.glob("*.tf") if path.is_file())
    requested = requested_provider_constraints(message)
    if files and requested and not any(path.name == "versions.tf" for path in files):
        declared = _declared_providers("\n".join(path.read_text(encoding="utf-8") for path in files))
        missing = {name: constraint for name, constraint in requested.items() if name not in declared}
        if missing:
            versions_path = workspace / "versions.tf"
            versions_path.write_text(_provider_stub_file(missing), encoding="utf-8")
            changed.append(versions_path.name)
            files.append(versions_path)
    for path in files:
        original = path.read_text(encoding="utf-8")
        updated = align_hcl(original, message, path.name, previous=original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            if path.name not in changed:
                changed.append(path.name)
    return changed


def rejected_hcl(content: str, message: str = "", filename: str = "") -> str | None:
    """Return a rejection reason when generated HCL breaks a hard safety rule."""
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
    provider_issue = _provider_version_issue(content, message, filename)
    if provider_issue:
        return provider_issue
    return engine_version_problem(content, message)


def engine_version_problem(terraform: str, message: str = "") -> str | None:
    """Reject a guessed RDS engine_version, or one other than the user requested."""
    versions = [
        match.group(1)
        for block in _RDS_ENGINE_VALUE.finditer(terraform)
        for match in _ENGINE_ASSIGNMENT.finditer(block.group("body"))
    ]
    if not versions:
        return None
    allowed = requested_engine_version(message)
    if allowed is None:
        return (
            "Rejected: do not hardcode aws_db_instance.engine_version. RDS engine "
            "versions vary by region and instance configuration; omit engine_version "
            "unless the user explicitly requested an exact version."
        )
    wrong = next((version for version in versions if version != allowed), None)
    if wrong:
        return (
            f"Rejected: RDS engine_version must be exactly {allowed}, "
            f"which the user requested. Found {wrong}."
        )
    return None


def _provider_version_issue(content: str, message: str, filename: str) -> str | None:
    declares_providers = "required_providers" in content or filename == "versions.tf"
    if not declares_providers:
        return None
    requested = requested_provider_constraints(message)
    declared = _declared_providers(content)
    if filename == "versions.tf":
        for name, constraint in requested.items():
            if name not in declared:
                return (
                    f"Rejected: versions.tf must set hashicorp/{name} to {constraint}, "
                    "because the user requested that version."
                )
    for name, actual in declared.items():
        expected = requested.get(name) or _DEFAULT_PROVIDER_VERSIONS.get(name)
        if expected is None:
            continue
        if actual is None or _normalize_constraint(actual) != _normalize_constraint(expected):
            found = actual or "no version"
            return (
                f"Rejected: hashicorp/{name} must use {expected}. Found {found}. "
                "Keep the default unless the user explicitly requested another version, "
                "and then write that requested constraint exactly."
            )
    return None


def _rewrite_provider_blocks(
    content: str,
    requested: dict[str, str],
    previous: dict[str, str | None],
) -> str:
    def replace(match: re.Match[str]) -> str:
        body = match.group("body")
        source = re.search(r'source\s*=\s*"hashicorp/([^"]+)"', body, re.IGNORECASE)
        if not source:
            return match.group(0)
        name = source.group(1).lower()
        expected = requested.get(name) or previous.get(name) or _DEFAULT_PROVIDER_VERSIONS.get(name)
        if expected is None:
            return match.group(0)
        current = re.search(r'version\s*=\s*"([^"]+)"', body)
        if current and _normalize_constraint(current.group(1)) == _normalize_constraint(expected):
            return match.group(0)
        if current:
            body = re.sub(
                r'(version\s*=\s*")[^"]+(")',
                rf"\g<1>{expected}\2",
                body,
                count=1,
            )
        else:
            body = body.rstrip("\n") + f'\n      version = "{expected}"\n    '
        return f'{match.group("block")} = {{{body}}}'

    return _PROVIDER_BLOCK.sub(replace, content)


def _insert_missing_providers(content: str, message: str) -> str:
    requested = requested_provider_constraints(message)
    declared = _declared_providers(content)
    missing = {name: constraint for name, constraint in requested.items() if name not in declared}
    if not missing:
        return content
    if "required_providers" not in content:
        return _provider_stub_file(missing) + content
    stubs = _provider_stubs(missing)
    return re.sub(
        r"(required_providers\s*\{)",
        lambda match: f"{match.group(1)}\n{stubs}",
        content,
        count=1,
    )


def _provider_stub_file(providers: dict[str, str]) -> str:
    return (
        "terraform {\n"
        "  required_providers {\n"
        f"{_provider_stubs(providers)}\n"
        "  }\n"
        "}\n\n"
    )


def _provider_stubs(providers: dict[str, str]) -> str:
    return "\n".join(
        "    "
        f"{name} = {{\n"
        f'      source  = "hashicorp/{name}"\n'
        f'      version = "{constraint}"\n'
        "    }"
        for name, constraint in providers.items()
    )


def _align_engine_version(content: str, message: str, previous: str) -> str:
    allowed = requested_engine_version(message)
    previous_versions = _engine_versions(previous)

    def replace(match: re.Match[str]) -> str:
        body = match.group("body")
        if allowed:
            if _ENGINE_ASSIGNMENT.search(body):
                body = re.sub(
                    r'(?m)^(\s*engine_version\s*=\s*")[^"]+(")',
                    rf"\g<1>{allowed}\2",
                    body,
                )
            else:
                body = body.rstrip("\n") + f'\n  engine_version = "{allowed}"\n'
        elif previous_versions:
            keep = previous_versions[0]
            if _ENGINE_ASSIGNMENT.search(body):
                body = re.sub(
                    r'(?m)^(\s*engine_version\s*=\s*")[^"]+(")',
                    rf"\g<1>{keep}\2",
                    body,
                )
        else:
            body = re.sub(r'(?m)^\s*engine_version\s*=\s*"[^"]+"\n?', "", body)
        return match.group(0).replace(match.group("body"), body, 1)

    return _RDS_ENGINE_VALUE.sub(replace, content)


def _engine_versions(content: str) -> list[str]:
    return [
        match.group(1)
        for block in _RDS_ENGINE_VALUE.finditer(content)
        for match in _ENGINE_ASSIGNMENT.finditer(block.group("body"))
    ]


def _declared_providers(content: str) -> dict[str, str | None]:
    declared: dict[str, str | None] = {}
    for match in _PROVIDER_BLOCK.finditer(content):
        body = match.group("body")
        source = re.search(r'source\s*=\s*"hashicorp/([^"]+)"', body, re.IGNORECASE)
        name = (source.group(1) if source else match.group("block")).lower()
        if name not in {"aws", "random", "archive"}:
            continue
        version = re.search(r'version\s*=\s*"([^"]+)"', body)
        declared[name] = version.group(1) if version else None
    return declared


def _normalize_constraint(value: str) -> str:
    compact = re.sub(r"\s+", "", value.strip())
    match = re.fullmatch(r"(~>|>=|=)?(\d+\.\d+(?:\.\d+)?)", compact)
    if not match:
        return compact
    operator = "" if match.group(1) in {None, "="} else match.group(1)
    return f"{operator} {match.group(2)}".strip()
