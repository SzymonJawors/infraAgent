import json
import secrets
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import PurePosixPath


GITHUB_API = "https://api.github.com"
GITHUB_AUTHORIZE = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN = "https://github.com/login/oauth/access_token"


@dataclass(frozen=True)
class GithubRepository:
    full_name: str
    default_branch: str
    private: bool
    html_url: str


class GithubError(RuntimeError):
    pass


def new_state() -> str:
    return secrets.token_urlsafe(32)


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": "repo",
        "state": state,
    })
    return f"{GITHUB_AUTHORIZE}?{query}"


def exchange_code(client_id: str, client_secret: str, code: str, redirect_uri: str) -> str:
    payload = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode()
    request = urllib.request.Request(
        GITHUB_TOKEN,
        data=payload,
        headers={"Accept": "application/json"},
        method="POST",
    )
    response = _open(request)
    token = response.get("access_token")
    if not isinstance(token, str) or not token:
        raise GithubError("GitHub did not return an access token")
    return token


def list_repositories(token: str) -> list[GithubRepository]:
    request = _request(token, "/user/repos?per_page=100&sort=updated")
    payload = _open(request)
    if not isinstance(payload, list):
        raise GithubError("GitHub returned an invalid repository list")
    return [
        GithubRepository(
            full_name=str(item["full_name"]),
            default_branch=str(item.get("default_branch") or "main"),
            private=bool(item.get("private")),
            html_url=str(item.get("html_url") or ""),
        )
        for item in payload
        if isinstance(item, dict) and item.get("full_name")
    ]


def terraform_files(token: str, full_name: str, branch: str) -> dict[str, str]:
    owner, repo = _repository_parts(full_name)
    tree = _open(_request(token, f"/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(branch, safe='') }?recursive=1"))
    entries = tree.get("tree", []) if isinstance(tree, dict) else []
    files = {}
    for entry in entries:
        path = entry.get("path") if isinstance(entry, dict) else None
        if (
            not isinstance(path, str)
            or not path.endswith(".tf")
            or entry.get("type") != "blob"
            or path.startswith("/")
            or any(part in {".", ".."} for part in PurePosixPath(path).parts)
        ):
            continue
        content = _open(_request(token, f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path, safe='')}?ref={urllib.parse.quote(branch, safe='')}"))
        encoded = content.get("content") if isinstance(content, dict) else None
        if isinstance(encoded, str):
            import base64
            files[path] = base64.b64decode(encoded.replace("\n", "")).decode("utf-8")
    return files


def analyze_repository(token: str, full_name: str, branch: str) -> dict[str, object]:
    """Build a bounded, read-only architecture summary from a repository tree."""
    owner, repo = _repository_parts(full_name)
    tree = _open(_request(
        token,
        f"/repos/{owner}/{repo}/git/trees/{urllib.parse.quote(branch, safe='')}?recursive=1",
    ))
    entries = tree.get("tree", []) if isinstance(tree, dict) else []
    paths = sorted(
        entry["path"]
        for entry in entries
        if isinstance(entry, dict)
        and entry.get("type") == "blob"
        and isinstance(entry.get("path"), str)
    )
    terraform_paths = [path for path in paths if path.endswith(".tf")]
    directory_names = sorted({
        part
        for path in paths
        for part in PurePosixPath(path).parts[:-1]
        if part not in {".git", ".github", "node_modules", ".terraform"}
    })
    readme = _read_text_file(token, owner, repo, _readme_path(paths), branch)
    technologies = _detect_technologies(paths, readme)
    has_terraform = bool(terraform_paths)
    deployable_stack = has_terraform or bool(technologies)
    if has_terraform:
        recommendation = (
            "Terraform is already present. Review the existing modules and propose a targeted "
            "infrastructure update instead of creating a duplicate stack."
        )
        suggested_prompt = (
            f"Analyze the existing infrastructure in {full_name} and propose a safe Terraform "
            "update based on the repository architecture. Preserve unrelated resources."
        )
    elif deployable_stack:
        recommendation = (
            "No Terraform configuration was detected. Propose a minimal AWS foundation that "
            "matches the detected application stack, then wait for review before applying it."
        )
        suggested_prompt = (
            f"Propose AWS Terraform infrastructure for {full_name} based on its detected "
            "application stack. Start with a minimal, production-sensible foundation and "
            "explain assumptions before writing files."
        )
    else:
        recommendation = (
            "No deployable application stack was detected. This repository appears to contain "
            "source or exercises without a recognizable deployment target. Infrastructure "
            "generation is not recommended until you specify what should be deployed."
        )
        suggested_prompt = (
            f"Explain why no deployable application stack was detected in {full_name}. "
            "Do not generate Terraform yet; ask what application and deployment target should "
            "be hosted on AWS."
        )
    return {
        "repository": full_name,
        "branch": branch,
        "has_terraform": has_terraform,
        "deployable_stack": deployable_stack,
        "terraform_files": terraform_paths[:100],
        "terraform_file_count": len(terraform_paths),
        "directories": directory_names[:100],
        "file_count": len(paths),
        "readme_excerpt": readme[:4000],
        "technologies": technologies,
        "recommendation": recommendation,
        "suggested_prompt": suggested_prompt,
    }


def _repository_parts(full_name: str) -> tuple[str, str]:
    owner, separator, repo = full_name.partition("/")
    if (
        not separator
        or not owner
        or not repo
        or "/" in repo
        or any(part in {".", ".."} for part in (owner, repo))
    ):
        raise GithubError("Invalid GitHub repository name")
    return owner, repo


def _readme_path(paths: list[str]) -> str | None:
    for candidate in ("README.md", "README.rst", "README.txt", "readme.md"):
        if candidate in paths:
            return candidate
    return None


def _read_text_file(
    token: str,
    owner: str,
    repo: str,
    path: str | None,
    branch: str,
) -> str:
    if not path:
        return ""
    content = _open(_request(
        token,
        f"/repos/{owner}/{repo}/contents/{urllib.parse.quote(path, safe='')}?ref={urllib.parse.quote(branch, safe='')}",
    ))
    encoded = content.get("content") if isinstance(content, dict) else None
    if not isinstance(encoded, str):
        return ""
    import base64
    try:
        return base64.b64decode(encoded.replace("\n", "")).decode("utf-8", errors="replace")
    except (ValueError, UnicodeDecodeError):
        return ""


def _detect_technologies(paths: list[str], readme: str) -> list[str]:
    names = {path.lower() for path in paths}
    text = readme.lower()
    detected: list[str] = []
    checks = (
        ("Docker", lambda: any(path.endswith(("dockerfile", ".dockerfile")) or "docker-compose" in path for path in names)),
        ("Node.js", lambda: "package.json" in names or "package-lock.json" in names),
        ("Python", lambda: "pyproject.toml" in names or "requirements.txt" in names or "setup.py" in names),
        ("Java", lambda: "pom.xml" in names or "build.gradle" in names),
        ("Go", lambda: "go.mod" in names),
        ("Kubernetes", lambda: any("k8s" in path or "kubernetes" in path for path in names)),
        ("Helm", lambda: "chart.yaml" in names or any("/templates/" in f"/{path}/" for path in names)),
        ("Serverless", lambda: any(name in names for name in ("serverless.yml", "serverless.yaml"))),
        ("AWS", lambda: "aws" in text or any("lambda" in path or "s3" in path for path in names)),
    )
    for label, check in checks:
        if check():
            detected.append(label)
    return detected


def _request(token: str, path: str) -> urllib.request.Request:
    return urllib.request.Request(
        f"{GITHUB_API}{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "InfraAgent",
        },
    )


def _open(request: urllib.request.Request) -> object:
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise GithubError("GitHub request failed") from exc
