"""Add labels to the release gate issue a Production build took.

Runs as the ``on_failure`` hook of the Production job's
``assert-gate-names-deploy`` task, adding ``ops:refused`` to the issue that
fired the gate. A refusal used to show only as a red build, which nothing
outside Concourse reads. The label puts it on the issue, where the release bot
and people looking at the issue see it.

It only ever adds labels. Each label records that something happened, so
several on one issue are expected (``ops:consumed`` and ``ops:refused`` mean
Production took the issue and refused it), and no writer removes one. Adding a
label is idempotent, so a retried build changes nothing. A label that is
missing from the repository is created first, since what GitHub does with an
unknown label name is undocumented. After the add, the issue is read again
and any label another writer removed is put back.

``pipeline.py`` inlines this file's text into the task (``python3 -c``), so it
must stay standard-library only and self-contained. It authenticates as the
release GitHub App the same way ``supersede_gate_issues.py`` does, signing the
app JWT with ``openssl``, which the task image carries.

Environment:
    GATE_FILE: ``<release-gate>/gh_issue.json`` from the gate get.
    ADD_LABELS: comma-separated label names to add.
    GITHUB_REPOSITORY: ``owner/repo`` holding the app's release issues.
    GITHUB_APP_ID, GITHUB_APP_INSTALLATION_ID, GITHUB_APP_PRIVATE_KEY: the
        release GitHub App's credentials.
    GITHUB_API_URL: optional, defaults to ``https://api.github.com``.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_ATTEMPTS = 3
#: Descriptions for labels this may have to create. Any other name gets none.
LABEL_DESCRIPTIONS = {
    "ops:refused": (
        "Production took this release issue and refused to deploy it: the "
        "approval no longer matches what would deploy."
    ),
}


def gate_issue_number(gate_file: Path) -> int:
    """Return the issue number the gate get recorded.

    Raises ValueError when there is none: the get writes ``{}`` for a version
    that names no issue.
    """
    number = int(json.loads(gate_file.read_text()).get("issue_number") or 0)
    if number <= 0:
        msg = f"{gate_file} names no issue"
        raise ValueError(msg)
    return number


def parse_labels(raw: str) -> list[str]:
    """Return the label names in *raw*, a comma-separated list, in order."""
    labels: list[str] = []
    for name in (part.strip() for part in raw.split(",")):
        if name and name not in labels:
            labels.append(name)
    return labels


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def app_jwt(app_id: str, private_key: str) -> str:
    """Return a GitHub App JWT signed RS256 with ``openssl``."""
    now = int(time.time())
    header = _b64(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    payload = _b64(
        json.dumps({"iat": now - 60, "exp": now + 540, "iss": app_id}).encode()
    )
    signing_input = f"{header}.{payload}".encode()
    with tempfile.NamedTemporaryFile("w", suffix=".pem") as key_file:
        key_file.write(private_key)
        key_file.flush()
        signature = subprocess.run(  # noqa: S603
            ["openssl", "dgst", "-sha256", "-sign", key_file.name],  # noqa: S607
            input=signing_input,
            capture_output=True,
            check=True,
        ).stdout
    return f"{header}.{payload}.{_b64(signature)}"


class GitHub:
    """The few GitHub REST calls this needs."""

    def __init__(self, api_url: str, token: str, *, scheme: str = "token") -> None:
        """Authenticate every request with *token* under *scheme*."""
        self.api_url = api_url.rstrip("/")
        self.auth = f"{scheme} {token}"

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> Any:
        """Send one request and return the decoded body."""
        request = urllib.request.Request(  # noqa: S310
            f"{self.api_url}{path}",
            method=method,
            data=None if body is None else json.dumps(body).encode(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": self.auth,
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "ol-concourse-label-gate-issue",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            raw = response.read()
            return json.loads(raw) if raw else None


def installation_token(
    api_url: str, app_id: str, installation_id: str, private_key: str
) -> str:
    """Exchange the app JWT for an installation token."""
    app = GitHub(api_url, app_jwt(app_id, private_key), scheme="Bearer")
    body = app.request("POST", f"/app/installations/{installation_id}/access_tokens")
    return str(body["token"])


def ensure_label(github: GitHub, repo: str, name: str) -> None:
    """Create the label *name* in *repo* unless it already exists (a 422)."""
    label = {"name": name, "color": "ededed"}
    if name in LABEL_DESCRIPTIONS:
        label["description"] = LABEL_DESCRIPTIONS[name]
    try:
        github.request("POST", f"/repos/{repo}/labels", label)
    except urllib.error.HTTPError as error:
        if error.code != 422:  # noqa: PLR2004
            raise


def add_labels(github: GitHub, repo: str, number: int, labels: list[str]) -> None:
    """Add *labels* to issue *number* and confirm they are there.

    Raises RuntimeError when a label keeps disappearing.
    """
    path = f"/repos/{repo}/issues/{number}"
    missing = labels
    for _ in range(_ATTEMPTS):
        github.request("POST", f"{path}/labels", {"labels": missing})
        current = {label["name"] for label in github.request("GET", path)["labels"]}
        missing = [name for name in labels if name not in current]
        if not missing:
            return
    msg = f"#{number}'s {missing} labels keep disappearing"
    raise RuntimeError(msg)


def main() -> None:
    """Add ``ADD_LABELS`` to the gate issue."""
    repo = os.environ["GITHUB_REPOSITORY"]
    api_url = os.environ.get("GITHUB_API_URL") or "https://api.github.com"
    labels = parse_labels(os.environ.get("ADD_LABELS", ""))
    if not labels:
        sys.exit("ADD_LABELS names no label.")
    try:
        number = gate_issue_number(Path(os.environ["GATE_FILE"]))
    except (ValueError, OSError) as error:
        sys.exit(f"Not labelling a gate issue: {error}.")

    token = installation_token(
        api_url,
        os.environ["GITHUB_APP_ID"],
        os.environ["GITHUB_APP_INSTALLATION_ID"],
        os.environ["GITHUB_APP_PRIVATE_KEY"],
    )
    github = GitHub(api_url, token)
    for name in labels:
        ensure_label(github, repo, name)
    add_labels(github, repo, number, labels)
    print(f"Labelled {repo}#{number} {', '.join(labels)}.")  # noqa: T201


if __name__ == "__main__":
    main()
