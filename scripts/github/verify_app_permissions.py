"""Phase 0 gate for the mitodl GitHub org import.

Two independent checks against the `ol-infrastructure-as-code` App installation:

1. `permissions` — diff the live grant against `docs/github-app-permissions.md` (audit rule
   SEC-12). Reports missing, over-granted, and unexpected entries.
2. `reads` — mint an installation token and exercise one read endpoint per resource type in
   plan section 3.3, proving the App can actually see what the import needs to import.

Read-only throughout. Never prints credential material.

    uv run python scripts/github/verify_app_permissions.py permissions
    uv run python scripts/github/verify_app_permissions.py reads
"""

import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import cyclopts
import httpx
import jwt

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from bridge.secrets.sops import read_yaml_secrets

app = cyclopts.App(help="Verify the ol-infrastructure-as-code GitHub App installation.")

API = "https://api.github.com"
ORG = "mitodl"
PROBE_REPO = "ol-infrastructure"

# Mirrors the tables in docs/github-app-permissions.md. Keep in sync -- this IS the SEC-12
# fixture. `organization_custom_roles` is deliberately absent: custom repository roles are an
# Enterprise-only feature and 404 on the Team plan.
EXPECTED: dict[str, str] = {
    # repository
    "administration": "write",
    "contents": "write",
    "workflows": "write",
    "secrets": "write",  # pragma: allowlist secret
    "actions_variables": "write",
    "dependabot_secrets": "write",  # pragma: allowlist secret
    "environments": "write",
    "issues": "write",
    "pages": "write",
    "repository_hooks": "write",
    "metadata": "read",
    "repository_custom_properties": "read",
    "vulnerability_alerts": "read",
    "secret_scanning_alerts": "read",  # pragma: allowlist secret
    "security_events": "read",
    "deployments": "read",
    "pull_requests": "read",
    # organization
    "organization_administration": "write",
    "members": "write",
    "organization_custom_properties": "admin",
    "organization_hooks": "write",
    "organization_secrets": "write",  # pragma: allowlist secret
    "organization_actions_variables": "write",
    "organization_self_hosted_runners": "write",
    "organization_custom_org_roles": "write",
    "organization_user_blocking": "write",
    "organization_plan": "read",
    "organization_personal_access_tokens": "read",
    "organization_personal_access_token_requests": "read",
    "organization_events": "read",
}

# (label, path). A resource type from plan section 3.3 -> one endpoint that reads it.
READ_CHECKS: list[tuple[str, str]] = [
    ("Repository", f"/repos/{ORG}/{PROBE_REPO}"),
    ("RepositoryTopics", f"/repos/{ORG}/{PROBE_REPO}/topics"),
    ("BranchDefault / Branch", f"/repos/{ORG}/{PROBE_REPO}/branches/main"),
    ("BranchProtection", f"/repos/{ORG}/{PROBE_REPO}/branches/main/protection"),
    ("RepositoryRuleset", f"/repos/{ORG}/{PROBE_REPO}/rulesets"),
    ("RepositoryCollaborators", f"/repos/{ORG}/{PROBE_REPO}/collaborators"),
    ("RepositoryWebhook", f"/repos/{ORG}/{PROBE_REPO}/hooks"),
    ("RepositoryDeployKey", f"/repos/{ORG}/{PROBE_REPO}/keys"),
    ("RepositoryEnvironment", f"/repos/{ORG}/{PROBE_REPO}/environments"),
    ("IssueLabels", f"/repos/{ORG}/{PROBE_REPO}/labels"),
    ("ActionsSecret (names only)", f"/repos/{ORG}/{PROBE_REPO}/actions/secrets"),
    ("ActionsVariable", f"/repos/{ORG}/{PROBE_REPO}/actions/variables"),
    ("DependabotSecret", f"/repos/{ORG}/{PROBE_REPO}/dependabot/secrets"),
    (
        "RepositoryVulnerabilityAlerts",
        f"/repos/{ORG}/{PROBE_REPO}/vulnerability-alerts",
    ),
    (
        "RepositoryDependabotSecurityUpdates",
        f"/repos/{ORG}/{PROBE_REPO}/automated-security-fixes",
    ),
    ("RepositoryCustomProperty", f"/repos/{ORG}/{PROBE_REPO}/properties/values"),
    ("RepositoryAutolinkReference", f"/repos/{ORG}/{PROBE_REPO}/autolinks"),
    ("Deployments (audit DX-08)", f"/repos/{ORG}/{PROBE_REPO}/deployments"),
    ("OrganizationSettings", f"/orgs/{ORG}"),
    ("OrganizationCustomProperties", f"/orgs/{ORG}/properties/schema"),
    ("OrganizationRuleset", f"/orgs/{ORG}/rulesets"),
    ("Team", f"/orgs/{ORG}/teams"),
    ("Membership", f"/orgs/{ORG}/members"),
    ("OrganizationWebhook", f"/orgs/{ORG}/hooks"),
    ("ActionsOrganizationSecret", f"/orgs/{ORG}/actions/secrets"),
    ("ActionsOrganizationVariable", f"/orgs/{ORG}/actions/variables"),
    ("ActionsRunnerGroup", f"/orgs/{ORG}/actions/runner-groups"),
    ("OrganizationRole", f"/orgs/{ORG}/organization-roles"),
    ("OrganizationBlock", f"/orgs/{ORG}/blocks"),
    ("Repo listing (all 317)", f"/orgs/{ORG}/repos?per_page=1"),
    ("PATs (audit)", f"/orgs/{ORG}/personal-access-tokens"),
    ("Installations (audit SEC-11/12)", f"/orgs/{ORG}/installations"),
]

HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}


def _user_token() -> str:
    """Return a user token with admin:org, for listing installations.

    Same lookup as github_recap.py, deliberately duplicated rather than imported: that
    module pulls in `ollama` at import time, which is optional and usually absent.
    """
    if token := os.getenv("GITHUB_TOKEN"):
        return token
    result = subprocess.run(
        ["gh", "auth", "token"],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _installation_token() -> str:
    """Mint a short-lived installation access token from the sops-held App key."""
    secrets = read_yaml_secrets(Path("pulumi/github_app.yaml"))
    now = int(time.time())
    assertion = jwt.encode(
        {"iat": now - 60, "exp": now + 540, "iss": str(secrets["app_id"])},
        secrets["private_key"],
        algorithm="RS256",
    )
    response = httpx.post(
        f"{API}/app/installations/{secrets['installation_id']}/access_tokens",
        headers={**HEADERS, "Authorization": f"Bearer {assertion}"},
        timeout=30,
    )
    response.raise_for_status()
    return str(response.json()["token"])


@app.command
def permissions() -> int:
    """Diff the live installation's permissions against the documented manifest (SEC-12)."""
    response = httpx.get(
        f"{API}/orgs/{ORG}/installations",
        headers={**HEADERS, "Authorization": f"Bearer {_user_token()}"},
        timeout=30,
    )
    if response.status_code != httpx.codes.OK:
        print(
            f"cannot list installations ({response.status_code}); needs `gh auth` admin:org"
        )
        return 1
    installation = next(
        (
            i
            for i in response.json()["installations"]
            if i["app_slug"] == "ol-infrastructure-as-code"
        ),
        None,
    )
    if installation is None:
        print(
            "ol-infrastructure-as-code is not installed on this org. "
            "That is itself the finding -- nothing in the import plan works without it."
        )
        return 1
    live: dict[str, Any] = installation["permissions"]

    missing = {k: v for k, v in EXPECTED.items() if k not in live}
    unexpected = {k: v for k, v in live.items() if k not in EXPECTED}
    wrong = {
        k: (EXPECTED[k], live[k])
        for k in EXPECTED.keys() & live.keys()
        if live[k] != EXPECTED[k]
    }

    for slug, level in sorted(missing.items()):
        print(f"MISSING     {slug}: {level}")
    for slug, (want, got) in sorted(wrong.items()):
        print(f"LEVEL       {slug}: expected {want}, live {got}")
    for slug, level in sorted(unexpected.items()):
        print(f"UNEXPECTED  {slug}: {level}  (not in the manifest)")

    total = len(missing) + len(wrong) + len(unexpected)
    print(
        f"\n{len(live)} live / {len(EXPECTED)} expected -- {total or 'no'} discrepancies"
    )
    return 1 if total else 0


@app.command
def reads() -> int:
    """Prove the installation can read every resource type the import touches."""
    token = _installation_token()
    auth = {**HEADERS, "Authorization": f"Bearer {token}"}
    failures = 0

    with httpx.Client(base_url=API, headers=auth, timeout=30) as client:
        for label, path in READ_CHECKS:
            status = client.get(path).status_code
            ok = status in (httpx.codes.OK, httpx.codes.NO_CONTENT)
            note = ""
            # 404 on branch protection means "none configured" -- a finding, not a perms error.
            if status == httpx.codes.NOT_FOUND and "protection" in path:
                ok, note = True, "  (404 = no protection configured; SEC-01 fires)"
            if not ok:
                failures += 1
            print(f"  {'OK  ' if ok else 'FAIL'} {status:3}  {label}{note}")

    print(f"\n{len(READ_CHECKS) - failures}/{len(READ_CHECKS)} readable")
    return 1 if failures else 0


if __name__ == "__main__":
    app()
