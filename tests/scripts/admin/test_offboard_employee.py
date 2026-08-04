"""Tests for scripts/admin/offboard-employee."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

SCRIPT_PATH = (
    Path(__file__).resolve().parents[3] / "scripts" / "admin" / "offboard-employee"
)


def load_offboard_module() -> Any:
    """Load the extensionless offboarding script as a Python module."""
    loader = importlib.machinery.SourceFileLoader(
        "test_offboard_employee", str(SCRIPT_PATH)
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        msg = f"Unable to load module from {SCRIPT_PATH}"
        raise RuntimeError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    loader.exec_module(module)
    return module


@pytest.fixture
def offboard_module() -> Any:
    """Return the loaded offboarding script module."""
    return load_offboard_module()


def write_iam_helper(repo_root: Path, username: str) -> None:
    """Write a minimal IAM helper fixture containing one managed username."""
    helper_path = repo_root / "src/ol_infrastructure/lib/aws/iam_helper.py"
    helper_path.parent.mkdir(parents=True)
    helper_path.write_text(f"ADMIN_USERNAMES = [{username!r}]\n")


class FakeProvider:
    """Provider test double that records whether revoke would execute."""

    name = "keycloak"

    def __init__(self, finding: Any):
        """Initialize the provider with one finding."""
        self.finding = finding
        self.execute_values: list[bool] = []
        self.discover_count = 0

    def discover(self, _identity: Any) -> list[Any]:
        """Return the configured finding."""
        self.discover_count += 1
        return [self.finding]

    def revoke(self, finding: Any, *, execute: bool) -> Any:
        """Record execute mode and return an action record."""
        self.execute_values.append(execute)
        return self.finding_module.api_record(
            finding,
            execute=execute,
            status=self.finding_module.ActionStatus.REVOKED
            if execute
            else self.finding_module.ActionStatus.WOULD_REVOKE,
        )

    @property
    def finding_module(self) -> Any:
        """Return the loaded script module."""
        return sys.modules["test_offboard_employee"]


class MissingProvider:
    """Provider test double that simulates absent credentials."""

    name = "rootly"

    def __init__(self, module: Any):
        """Initialize with the loaded script module."""
        self.module = module

    def discover(self, _identity: Any) -> list[Any]:
        """Raise the same missing-credential error real providers raise."""
        msg = "missing token"
        raise self.module.OffboardingError(msg)

    def revoke(self, _finding: Any, *, execute: bool) -> Any:
        """Fail if revoke is called after failed discovery."""
        msg = f"revoke should not be called: {execute}"
        raise AssertionError(msg)


@pytest.mark.unit
def test_collect_action_records_is_dry_run_by_default(offboard_module: Any) -> None:
    """Provider revoke receives execute=False for the default safety path."""
    identity = offboard_module.Identity("user@example.com")
    finding = offboard_module.Finding(
        service="keycloak",
        identity=identity,
        target_id="user-id",
        target_label="user@example.com",
        verb="disable user",
        outcome=offboard_module.FindingOutcome.REVOKED_VIA_API,
        request={"method": "PUT"},
    )
    provider = FakeProvider(finding)

    records = offboard_module.collect_action_records(
        [identity], [provider], execute=False
    )

    assert provider.execute_values == [False]
    assert records[0].status is offboard_module.ActionStatus.WOULD_REVOKE
    assert not records[0].execute


@pytest.mark.unit
def test_missing_provider_credentials_are_skipped_without_aborting(
    offboard_module: Any,
) -> None:
    """A missing service credential yields a skipped record and keeps going."""
    identity = offboard_module.Identity("user@example.com")
    providers = [
        MissingProvider(offboard_module),
        offboard_module.ManualTailProvider(repo_root=offboard_module.REPO_ROOT),
    ]

    records = offboard_module.collect_action_records(
        [identity], providers, execute=False
    )

    assert records[-1].service == "rootly"
    assert records[-1].status is offboard_module.ActionStatus.SKIPPED
    assert any(record.service == "human" for record in records)


@pytest.mark.unit
def test_selected_providers_support_comma_filters(offboard_module: Any) -> None:
    """--only and --skip accept comma-separated service names."""
    providers = offboard_module.build_providers(
        repo_root=Path.cwd(),
        keycloak_url="https://sso.example.invalid",
        keycloak_realm="realm",
        keycloak_auth_realm="master",
        keycloak_username="admin",
        keycloak_password=None,
        vault_addr=None,
        vault_token=None,
        aws_profile=None,
        aws_account_id=None,
        rootly_token=None,
    )

    only = offboard_module.parse_service_filter(["aws,repo-code"])
    selected = offboard_module.selected_providers(providers, only, set())

    assert [provider.name for provider in selected] == ["aws", "aws", "repo-code"]


@pytest.mark.unit
def test_aws_code_change_discovery_reports_exact_iam_helper_symbol(
    offboard_module: Any, tmp_path: Path
) -> None:
    """AWS Pulumi-managed access is reported as code cleanup, not API deletion."""
    write_iam_helper(tmp_path, "testuser")
    provider = offboard_module.AWSProvider(repo_root=tmp_path)
    identity = offboard_module.Identity("testuser@mit.edu")

    findings = provider._code_change_findings(identity)

    symbols = {
        finding.code_change.symbol for finding in findings if finding.code_change
    }
    assert symbols == {"ADMIN_USERNAMES"}
    assert all(
        finding.outcome is offboard_module.FindingOutcome.NEEDS_CODE_CHANGE
        for finding in findings
    )


@pytest.mark.unit
def test_repo_code_provider_finds_hardcoded_email(
    offboard_module: Any, tmp_path: Path
) -> None:
    """Repository scanner reports scattered hardcoded email references."""
    target = tmp_path / "docs" / "example.md"
    target.parent.mkdir()
    target.write_text("contact former.user@example.com for access\n")
    provider = offboard_module.RepoCodeProvider(repo_root=tmp_path)

    findings = provider.discover(offboard_module.Identity("former.user@example.com"))

    assert len(findings) == 1
    assert findings[0].code_change.path == "docs/example.md"
    assert findings[0].outcome is offboard_module.FindingOutcome.NEEDS_CODE_CHANGE


@pytest.mark.unit
def test_keycloak_discover_and_dry_run_revoke_use_httpx_without_mutation(
    offboard_module: Any,
) -> None:
    """Keycloak dry-run discovers users but does not send mutating requests."""
    seen_methods: list[str] = []

    def handler(request: Any) -> Any:
        seen_methods.append(request.method)
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return offboard_module.httpx.Response(200, json={"access_token": "token"})
        if request.url.path.endswith("/admin/realms/ol-platform-engineering/users"):
            return offboard_module.httpx.Response(
                200,
                json=[
                    {
                        "id": "keycloak-user-id",
                        "email": "user@example.com",
                        "username": "user@example.com",
                        "enabled": True,
                    }
                ],
            )
        return offboard_module.httpx.Response(
            500, json={"unexpected": request.url.path}
        )

    client = offboard_module.httpx.Client(
        transport=offboard_module.httpx.MockTransport(handler),
        base_url="https://sso.example.invalid",
    )
    provider = offboard_module.KeycloakProvider(
        base_url="https://sso.example.invalid",
        realm="ol-platform-engineering",
        auth_realm="master",
        username="admin",
        password="password",  # pragma: allowlist secret
        client=client,
    )

    findings = provider.discover(offboard_module.Identity("user@example.com"))
    record = provider.revoke(findings[0], execute=False)

    assert seen_methods == ["POST", "GET"]
    assert record.status is offboard_module.ActionStatus.WOULD_REVOKE
    assert record.target_id == "keycloak-user-id"


@pytest.mark.unit
def test_keycloak_execute_revokes_user_mappings_and_federated_identities(  # noqa: C901
    offboard_module: Any,
) -> None:
    """Keycloak execute sends the destructive requests against the mock API."""
    seen: list[tuple[str, str]] = []

    def handler(request: Any) -> Any:  # noqa: PLR0911
        seen.append((request.method, request.url.path))
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return offboard_module.httpx.Response(200, json={"access_token": "token"})
        if request.method == "GET" and request.url.path.endswith(
            "/admin/realms/ol-platform-engineering/users"
        ):
            return offboard_module.httpx.Response(
                200,
                json=[
                    {
                        "id": "keycloak-user-id",
                        "email": "user@example.com",
                        "username": "user@example.com",
                        "enabled": True,
                    }
                ],
            )
        if request.method == "PUT" and request.url.path.endswith(
            "/users/keycloak-user-id"
        ):
            payload = offboard_module.json.loads(request.content)
            assert payload == {"enabled": False}
            return offboard_module.httpx.Response(204)
        if request.method == "POST" and request.url.path.endswith(
            "/users/keycloak-user-id/logout"
        ):
            return offboard_module.httpx.Response(204)
        if request.method == "GET" and request.url.path.endswith(
            "/users/keycloak-user-id/groups"
        ):
            return offboard_module.httpx.Response(200, json=[{"id": "group-uuid"}])
        if request.method == "GET" and request.url.path.endswith(
            "/users/keycloak-user-id/role-mappings"
        ):
            return offboard_module.httpx.Response(
                200,
                json={
                    "realmMappings": [{"id": "realm-role", "name": "admin"}],
                    "clientMappings": {
                        "ol-vault-client": {
                            "id": "client-uuid",
                            "mappings": [{"id": "client-role", "name": "admin"}],
                        }
                    },
                },
            )
        if request.method == "GET" and request.url.path.endswith(
            "/users/keycloak-user-id/consents"
        ):
            return offboard_module.httpx.Response(
                200, json=[{"clientId": "offline-client"}]
            )
        if request.method == "DELETE" and request.url.path.endswith(
            (
                "/users/keycloak-user-id/groups/group-uuid",
                "/users/keycloak-user-id/role-mappings/realm",
                "/users/keycloak-user-id/role-mappings/clients/client-uuid",
                "/users/keycloak-user-id/consents/offline-client",
                "/users/keycloak-user-id/federated-identity/github",
            )
        ):
            return offboard_module.httpx.Response(204)
        if request.method == "GET" and request.url.path.endswith(
            "/users/keycloak-user-id/federated-identity"
        ):
            return offboard_module.httpx.Response(
                200, json=[{"identityProvider": "github"}]
            )
        return offboard_module.httpx.Response(
            500, json={"unexpected": f"{request.method} {request.url.path}"}
        )

    client = offboard_module.httpx.Client(
        transport=offboard_module.httpx.MockTransport(handler),
        base_url="https://sso.example.invalid",
    )
    provider = offboard_module.KeycloakProvider(
        base_url="https://sso.example.invalid",
        realm="ol-platform-engineering",
        auth_realm="master",
        username="admin",
        password="password",  # pragma: allowlist secret
        client=client,
    )

    findings = provider.discover(offboard_module.Identity("user@example.com"))
    record = provider.revoke(findings[0], execute=True)

    assert record.status is offboard_module.ActionStatus.REVOKED
    assert seen.count(("POST", "/realms/master/protocol/openid-connect/token")) == 2
    assert (
        "PUT",
        "/admin/realms/ol-platform-engineering/users/keycloak-user-id",
    ) in seen
    assert (
        "DELETE",
        "/admin/realms/ol-platform-engineering/users/keycloak-user-id/role-mappings/realm",
    ) in seen
    assert (
        "DELETE",
        "/admin/realms/ol-platform-engineering/users/keycloak-user-id/federated-identity/github",
    ) in seen


@pytest.mark.unit
def test_vault_discover_and_revoke_live_oidc_token_accessors(
    offboard_module: Any,
) -> None:
    """Vault provider maps email aliases to token accessors without mutation."""
    seen: list[tuple[str, str]] = []

    def handler(request: Any) -> Any:  # noqa: PLR0911
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/v1/sys/auth":
            return offboard_module.httpx.Response(
                200, json={"data": {"oidc/": {"accessor": "oidc-accessor"}}}
            )
        if (
            request.method == "POST"
            and request.url.path == "/v1/identity/lookup/entity"
        ):
            return offboard_module.httpx.Response(
                200, json={"data": {"id": "entity-1"}}
            )
        if request.method == "LIST" and request.url.path == "/v1/auth/token/accessors":
            return offboard_module.httpx.Response(
                200, json={"data": {"keys": ["live-accessor", "other-accessor"]}}
            )
        if (
            request.method == "POST"
            and request.url.path == "/v1/auth/token/lookup-accessor"
        ):
            accessor = offboard_module.json.loads(request.content)["accessor"]
            entity_id = "entity-1" if accessor == "live-accessor" else "entity-2"
            return offboard_module.httpx.Response(
                200, json={"data": {"entity_id": entity_id}}
            )
        if (
            request.method == "POST"
            and request.url.path == "/v1/auth/token/revoke-accessor"
        ):
            return offboard_module.httpx.Response(204)
        if (
            request.method == "POST"
            and request.url.path == "/v1/identity/entity/id/entity-1"
        ):
            assert offboard_module.json.loads(request.content) == {"disabled": True}
            return offboard_module.httpx.Response(204)
        return offboard_module.httpx.Response(
            500, json={"unexpected": f"{request.method} {request.url.path}"}
        )

    client = offboard_module.httpx.Client(
        transport=offboard_module.httpx.MockTransport(handler),
        base_url="https://vault.example.invalid",
    )
    provider = offboard_module.VaultProvider(
        address="https://vault.example.invalid",
        token="test-token",
        client=client,
    )

    findings = provider.discover(offboard_module.Identity("user@example.com"))
    dry_run_record = provider.revoke(findings[0], execute=False)
    execute_record = provider.revoke(findings[0], execute=True)
    entity_record = provider.revoke(findings[1], execute=True)

    assert [finding.target_id for finding in findings] == [
        "live-accessor",
        "entity-1",
    ]
    assert dry_run_record.status is offboard_module.ActionStatus.WOULD_REVOKE
    assert execute_record.status is offboard_module.ActionStatus.REVOKED
    assert entity_record.status is offboard_module.ActionStatus.REVOKED
    assert ("POST", "/v1/auth/token/revoke-accessor") in seen
    assert ("POST", "/v1/identity/entity/id/entity-1") in seen


@pytest.mark.unit
def test_rootly_discovers_api_deactivation_code_change_and_handoff(
    offboard_module: Any, tmp_path: Path
) -> None:
    """Rootly provider splits API deactivation from Pulumi and human follow-up."""
    rootly_file = (
        tmp_path / "src" / "ol_infrastructure" / "saas" / "rootly" / "__main__.py"
    )
    rootly_file.parent.mkdir(parents=True)
    rootly_file.write_text(
        "user_ids=[\n"
        "    99415,\n"
        "]\n"
        "schedule_rotation_members=[\n"
        '    {"memberId": "99415", "position": 1},\n'
        "]\n"
        "notification_target_params=[\n"
        '    {"id": "99415", "type": "user"},\n'
        "]\n"
    )
    seen: list[tuple[str, str]] = []

    def handler(request: Any) -> Any:
        seen.append((request.method, request.url.path))
        if request.method == "GET" and request.url.path == "/v1/users":
            return offboard_module.httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "id": "99415",
                            "attributes": {
                                "email": "user@example.com",
                                "name": "Example User",
                            },
                        },
                        {
                            "id": "99999",
                            "attributes": {
                                "email": "unrelated@example.com",
                                "name": "Unrelated User",
                            },
                        },
                    ]
                },
            )
        if request.method == "PATCH" and request.url.path == "/v1/users/99415":
            payload = offboard_module.json.loads(request.content)
            assert payload["data"] == {
                "id": "99415",
                "type": "users",
                "attributes": {"active": False},
            }
            return offboard_module.httpx.Response(200, json={"data": {"id": "99415"}})
        if request.method == "GET" and request.url.path == "/v1/users/99415":
            return offboard_module.httpx.Response(
                200,
                json={
                    "data": {
                        "id": "99415",
                        "attributes": {"active": False},
                    }
                },
            )
        return offboard_module.httpx.Response(
            500, json={"unexpected": f"{request.method} {request.url.path}"}
        )

    client = offboard_module.httpx.Client(
        transport=offboard_module.httpx.MockTransport(handler),
        base_url="https://rootly.example.invalid",
    )
    provider = offboard_module.RootlyProvider(
        repo_root=tmp_path,
        token="test-token",
        base_url="https://rootly.example.invalid",
        client=client,
    )

    findings = provider.discover(offboard_module.Identity("user@example.com"))
    api_finding = next(
        finding
        for finding in findings
        if finding.outcome is offboard_module.FindingOutcome.REVOKED_VIA_API
    )
    dry_run_record = provider.revoke(api_finding, execute=False)
    execute_record = provider.revoke(api_finding, execute=True)

    assert dry_run_record.status is offboard_module.ActionStatus.WOULD_REVOKE
    assert execute_record.status is offboard_module.ActionStatus.REVOKED
    assert not any(finding.target_id == "99999" for finding in findings)
    assert {finding.outcome for finding in findings} == {
        offboard_module.FindingOutcome.REVOKED_VIA_API,
        offboard_module.FindingOutcome.NEEDS_CODE_CHANGE,
        offboard_module.FindingOutcome.NEEDS_HUMAN,
    }
    code_changes = [
        finding.code_change for finding in findings if finding.code_change is not None
    ]
    assert {change.symbol for change in code_changes} == {
        "team_platform_engineering.user_ids",
        "schedule_rotation_members",
        "notification_target_params",
    }
    assert any("URGENT" in change.instruction for change in code_changes)
    assert any("REFUSE" in change.instruction for change in code_changes)
    assert ("PATCH", "/v1/users/99415") in seen
    assert ("GET", "/v1/users/99415") in seen


@pytest.mark.unit
def test_non_mit_email_never_derives_an_aws_revocation_target(
    offboard_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A contractor email remains manual and never reaches the AWS API."""
    provider = offboard_module.AWSEphemeralCredentialProvider(repo_root=tmp_path)
    monkeypatch.setattr(
        provider,
        "_iam_client",
        lambda: pytest.fail("non-MIT identity called the AWS API"),
    )

    findings = provider.discover(
        offboard_module.Identity("arslan.abdulrauf@arbisoft.com")
    )
    record = provider.revoke(findings[0], execute=False)

    assert findings[0].outcome is offboard_module.FindingOutcome.NEEDS_HUMAN
    assert record.status is offboard_module.ActionStatus.INCOMPLETE
    assert record.target_id == "arslan.abdulrauf"
    assert offboard_module.records_exit_code([record], execute=False) == 2


@pytest.mark.unit
def test_aws_virtual_mfa_is_deactivated_and_deleted(
    offboard_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Virtual MFA resources are deleted after they are deactivated."""

    class FakeIAM:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def list_access_keys(self, **_kwargs: Any) -> dict[str, list[Any]]:
            return {"AccessKeyMetadata": []}

        def get_login_profile(self, **_kwargs: Any) -> None:
            raise offboard_module.ClientError(
                {"Error": {"Code": "NoSuchEntity", "Message": "missing"}},
                "GetLoginProfile",
            )

        def list_mfa_devices(self, **_kwargs: Any) -> dict[str, list[Any]]:
            return {
                "MFADevices": [{"SerialNumber": "arn:aws:iam::123456789012:mfa/user"}]
            }

        def deactivate_mfa_device(self, **kwargs: Any) -> None:
            self.calls.append(("deactivate", kwargs["SerialNumber"]))

        def delete_virtual_mfa_device(self, **kwargs: Any) -> None:
            self.calls.append(("delete", kwargs["SerialNumber"]))

    iam = FakeIAM()
    provider = offboard_module.AWSEphemeralCredentialProvider(repo_root=Path.cwd())
    monkeypatch.setattr(provider, "_iam_client", lambda: iam)
    findings = provider._ephemeral_credential_findings(
        offboard_module.Identity("user@mit.edu"),
        iam,
        scope="AWS account=123456789012 profile=test",
    )
    mfa_finding = next(
        finding
        for finding in findings
        if finding.outcome is offboard_module.FindingOutcome.REVOKED_VIA_API
    )

    record = provider.revoke(mfa_finding, execute=True)

    assert record.status is offboard_module.ActionStatus.REVOKED
    assert iam.calls == [
        ("deactivate", "arn:aws:iam::123456789012:mfa/user"),
        ("delete", "arn:aws:iam::123456789012:mfa/user"),
    ]
    assert any(
        finding.outcome is offboard_module.FindingOutcome.NEEDS_HUMAN
        for finding in findings
    )


@pytest.mark.unit
def test_aws_code_change_revoke_never_calls_iam(
    offboard_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pulumi-managed AWS findings remain reports even in execute mode."""
    write_iam_helper(tmp_path, "testuser")
    provider = offboard_module.AWSProvider(repo_root=tmp_path)
    monkeypatch.setattr(
        provider,
        "_iam_client",
        lambda: pytest.fail("Pulumi-managed finding called IAM"),
    )
    finding = provider._code_change_findings(
        offboard_module.Identity("testuser@mit.edu")
    )[0]

    record = provider.revoke(finding, execute=True)

    assert record.status is offboard_module.ActionStatus.REPORTED
    assert record.outcome is offboard_module.FindingOutcome.NEEDS_CODE_CHANGE


@pytest.mark.unit
def test_identity_rejects_non_email_input(offboard_module: Any) -> None:
    """Ambiguous localparts cannot reach the AWS provider as usernames."""
    with pytest.raises(ValueError, match="Invalid email address"):
        offboard_module.Identity("admin")


@pytest.mark.unit
def test_execute_uses_the_exact_confirmed_discovery(offboard_module: Any) -> None:
    """Execution reuses one discovery result instead of discovering a new plan."""
    identity = offboard_module.Identity("user@example.com")
    finding = offboard_module.Finding(
        service="keycloak",
        identity=identity,
        target_id="user-id",
        target_label=identity.email,
        verb="disable user",
        outcome=offboard_module.FindingOutcome.REVOKED_VIA_API,
        scope="https://sso.example.invalid realm=realm",
    )
    provider = FakeProvider(finding)
    discovered, discovery_records = offboard_module.discover_provider_findings(
        [identity], [provider]
    )
    dry_run = offboard_module.records_for_discovery(
        discovered, discovery_records, execute=False
    )
    executed = offboard_module.records_for_discovery(
        discovered, discovery_records, execute=True
    )

    assert provider.discover_count == 1
    assert provider.execute_values == [False, True]
    assert dry_run[0].target_id == executed[0].target_id == "user-id"
    assert offboard_module.confirmation_summary(dry_run).splitlines()[1] == (
        "- user@example.com: keycloak "
        "[https://sso.example.invalid realm=realm] -> user@example.com (disable user)"
    )


@pytest.mark.unit
def test_keycloak_no_exact_match_is_incomplete(offboard_module: Any) -> None:
    """A missing Keycloak identity cannot silently disappear from the report."""

    def handler(request: Any) -> Any:
        if request.url.path.endswith("/protocol/openid-connect/token"):
            return offboard_module.httpx.Response(200, json={"access_token": "token"})
        return offboard_module.httpx.Response(
            200,
            json=[
                {
                    "id": "wrong-user",
                    "email": "other@example.com",
                    "username": "other@example.com",
                }
            ],
        )

    provider = offboard_module.KeycloakProvider(
        base_url="https://sso.example.invalid",
        realm="ol-platform-engineering",
        auth_realm="master",
        username="admin",
        password="password",  # pragma: allowlist secret
        client=offboard_module.httpx.Client(
            transport=offboard_module.httpx.MockTransport(handler)
        ),
    )

    finding = provider.discover(offboard_module.Identity("user@example.com"))[0]
    record = provider.revoke(finding, execute=False)

    assert finding.outcome is offboard_module.FindingOutcome.NEEDS_HUMAN
    assert record.status is offboard_module.ActionStatus.INCOMPLETE
    assert offboard_module.records_exit_code([record], execute=False) == 2


@pytest.mark.unit
def test_nonproduction_keycloak_cannot_execute(offboard_module: Any) -> None:
    """QA remains a read-only discovery target even with --execute."""
    provider = offboard_module.KeycloakProvider(
        base_url="https://sso-qa.ol.mit.edu",
        realm="ol-platform-engineering",
        auth_realm="master",
        username="admin",
        password="password",  # pragma: allowlist secret
    )

    with pytest.raises(RuntimeError, match="Refusing --execute"):
        offboard_module.validate_execution_targets([provider])


@pytest.mark.unit
def test_aws_execute_requires_explicit_account_id(
    offboard_module: Any, tmp_path: Path
) -> None:
    """Ambient AWS credentials cannot mutate an unconfirmed account."""
    provider = offboard_module.AWSEphemeralCredentialProvider(repo_root=tmp_path)

    with pytest.raises(RuntimeError, match="--aws-account-id"):
        offboard_module.validate_execution_targets([provider])

    provider.expected_account_id = "not-an-account"
    with pytest.raises(RuntimeError, match="12-digit"):
        offboard_module.validate_execution_targets([provider])

    provider.expected_account_id = "123456789012"
    offboard_module.validate_execution_targets([provider])


@pytest.mark.unit
def test_aws_discovery_rejects_an_unexpected_account(
    offboard_module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Discovery fails closed when ambient credentials target another AWS account."""

    class FakeIAM:
        def get_user(self, **_kwargs: Any) -> dict[str, Any]:
            return {"User": {"Arn": "arn:aws:iam::999999999999:user/testuser"}}

    provider = offboard_module.AWSEphemeralCredentialProvider(
        repo_root=tmp_path,
        expected_account_id="123456789012",
    )
    monkeypatch.setattr(provider, "_iam_client", FakeIAM)

    with pytest.raises(
        offboard_module.OffboardingError, match="not explicitly confirmed"
    ):
        provider.discover(offboard_module.Identity("testuser@mit.edu"))


@pytest.mark.unit
def test_login_profile_access_denied_is_not_reported_as_absent(
    offboard_module: Any,
) -> None:
    """AWS authorization failures cannot masquerade as an absent login profile."""

    class AccessDeniedIAM:
        def get_login_profile(self, **_kwargs: Any) -> None:
            raise offboard_module.ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "denied"}},
                "GetLoginProfile",
            )

    with pytest.raises(offboard_module.ClientError):
        offboard_module.has_login_profile(AccessDeniedIAM(), "user")


@pytest.mark.unit
def test_records_exit_nonzero_when_provider_is_incomplete(offboard_module: Any) -> None:
    """Skipped and failed providers are visible to shell automation."""
    identity = offboard_module.Identity("user@example.com")
    skipped = offboard_module.skipped_record("vault", identity, "missing token")
    finding = offboard_module.Finding(
        service="keycloak",
        identity=identity,
        target_id="user-id",
        target_label=identity.email,
        verb="disable user",
        outcome=offboard_module.FindingOutcome.REVOKED_VIA_API,
    )
    failed = offboard_module.error_record(finding, "request failed", execute=True)

    incomplete = offboard_module.report_record(
        offboard_module.vault_unresolved_identity_finding(
            identity, "https://vault.example.invalid"
        ),
        execute=False,
    )

    assert offboard_module.records_exit_code([skipped], execute=False) == 2
    assert offboard_module.records_exit_code([incomplete], execute=False) == 2
    assert offboard_module.records_exit_code([failed], execute=True) == 1


@pytest.mark.unit
def test_confirmation_warns_about_incomplete_discovery(offboard_module: Any) -> None:
    """The destructive prompt makes unchecked providers explicit."""
    identity = offboard_module.Identity("user@example.com")
    incomplete = offboard_module.report_record(
        offboard_module.rootly_unresolved_identity_finding(
            identity, "https://api.rootly.example.invalid"
        ),
        execute=False,
    )

    summary = offboard_module.confirmation_summary([incomplete])

    assert "Discovery is incomplete" in summary
    assert "[incomplete] user@example.com: rootly" in summary
    assert "known revocations" in summary


@pytest.mark.unit
def test_confirmation_must_match_exactly(
    offboard_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A mistyped destructive confirmation aborts before execution."""
    monkeypatch.setattr("builtins.input", lambda: "execute")

    with pytest.raises(RuntimeError, match="aborting without mutations"):
        offboard_module.require_confirmation([])


@pytest.mark.unit
def test_manual_tail_derives_console_urls_from_stack_config(
    offboard_module: Any, tmp_path: Path
) -> None:
    """Grafana and Atlas consoles come from stack config, not hardcoded hostnames."""
    keycloak_dir = tmp_path / "src/ol_infrastructure/substructure/keycloak"
    keycloak_dir.mkdir(parents=True)
    (keycloak_dir / "Pulumi.Production.yaml").write_text(
        "config:\n"
        "  keycloak_realm:ol-platform-engineering-grafana-web-origins:"
        ' ["https://example-prod.grafana.net"]\n'
    )
    atlas_dir = tmp_path / "src/ol_infrastructure/infrastructure/mongodb_atlas"
    atlas_dir.mkdir(parents=True)
    (atlas_dir / "Pulumi.mitx.Production.yaml").write_text(
        "config:\n  mongodb_atlas:organization_id: abc123\n"
    )

    findings = offboard_module.ManualTailProvider(repo_root=tmp_path).discover(
        offboard_module.Identity("user@example.com")
    )
    consoles = {
        finding.target_id.split(":", 1)[0]: finding.human_action.console_url
        for finding in findings
    }

    assert consoles["grafana"] == "https://example-prod.grafana.net/admin/users"
    assert (
        consoles["mongodb-atlas"]
        == "https://cloud.mongodb.com/v2#/org/abc123/access/users"
    )


@pytest.mark.unit
def test_manual_tail_console_urls_are_real_consoles(offboard_module: Any) -> None:
    """No manual action may point the operator at a docs or runbook repository."""
    findings = offboard_module.ManualTailProvider(
        repo_root=offboard_module.REPO_ROOT
    ).discover(offboard_module.Identity("user@example.com"))
    consoles = [
        finding.human_action.console_url
        for finding in findings
        if finding.human_action.console_url
    ]

    parsed = [urlparse(console) for console in consoles]

    assert consoles
    assert all(url.scheme == "https" for url in parsed)
    assert all(url.hostname != "github.mit.edu" for url in parsed)
    assert not any("runbook" in url.path for url in parsed)


@pytest.mark.unit
def test_manual_tail_omits_console_url_it_cannot_derive(offboard_module: Any) -> None:
    """An undiscoverable vendor yields no link rather than a misleading one."""
    findings = offboard_module.ManualTailProvider(
        repo_root=offboard_module.REPO_ROOT
    ).discover(offboard_module.Identity("user@example.com"))
    password_manager = next(
        finding
        for finding in findings
        if finding.target_id.startswith("password-manager:")
    )
    record = offboard_module.report_record(password_manager, execute=False)

    assert password_manager.human_action.console_url is None
    assert "console:" not in offboard_module.records_to_text([record], execute=False)
