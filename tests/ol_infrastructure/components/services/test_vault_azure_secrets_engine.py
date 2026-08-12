"""Tests for the OLVaultAzureSecretsEngine Pulumi component.

The component exists mainly to keep callers away from two API details of
``pulumi_vault.azure`` that differ from the AWS secrets engine sitting next to
it in the same module, and that fail quietly rather than loudly:

1. ``azure.BackendRole`` identifies the role with ``role=``, not the ``name=``
   used by ``aws.SecretBackendRole``. Getting this wrong does not raise -- it
   creates a role under an auto-generated name, and the failure only shows up
   later as a missing role at credential-request time.
2. Role ``ttl`` / ``max_ttl`` are Go duration strings ("24h"), where the AWS
   engine's mount-level equivalents are integer seconds. An int here is a type
   error at apply time at best, and a nonsense lease at worst.

So the assertions below are deliberately about the wire-level inputs rather
than about the config model round-tripping.
"""

from __future__ import annotations

import asyncio

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class VaultMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(VaultMocks())

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from ol_infrastructure.components.services.vault import (  # noqa: E402
    OLVaultAzureRoleConfig,
    OLVaultAzureSecretsEngine,
    OLVaultAzureSecretsEngineConfig,
)

COGNITIVE_ACCOUNT_SCOPE = (
    "/subscriptions/00000000-0000-0000-0000-000000000000"
    "/resourceGroups/ol-openai-ci"
    "/providers/Microsoft.CognitiveServices/accounts/ol-openai-mitlearn-ci"
)


def _engine_config(**overrides) -> OLVaultAzureSecretsEngineConfig:
    defaults = {
        "app_name": "openai",
        "subscription_id": "00000000-0000-0000-0000-000000000000",
        "tenant_id": "11111111-1111-1111-1111-111111111111",
        "client_id": "22222222-2222-2222-2222-222222222222",
        "client_secret": "not-a-real-secret",  # pragma: allowlist secret
        "description": "Azure OpenAI dynamic credentials",
        "roles": {
            "ol-mitlearn-openai": OLVaultAzureRoleConfig(
                role_name="Cognitive Services OpenAI User",
                scope=COGNITIVE_ACCOUNT_SCOPE,
            )
        },
    }
    return OLVaultAzureSecretsEngineConfig(**(defaults | overrides))


# ─── Config validation ────────────────────────────────────────────────────────


def test_default_mount_path_is_azure_openai():
    assert _engine_config().vault_backend_path == "azure-openai"


@pytest.mark.parametrize("bad_path", ["/azure-openai", "azure-openai/"])
def test_mount_path_rejects_leading_or_trailing_slash(bad_path):
    with pytest.raises(ValidationError, match="can not start or end with a slash"):
        _engine_config(vault_backend_path=bad_path)


def test_role_ttls_default_to_duration_strings():
    """Not ints. Azure AD SP creation is eventually consistent, hence hours."""
    role = OLVaultAzureRoleConfig(
        role_name="Cognitive Services OpenAI User",
        scope=COGNITIVE_ACCOUNT_SCOPE,
    )
    assert role.ttl == "24h"
    assert role.max_ttl == "48h"


# ─── Rendered backend ─────────────────────────────────────────────────────────


@pulumi.runtime.test
def test_backend_mounts_at_configured_path_with_credentials():
    engine = OLVaultAzureSecretsEngine(_engine_config())

    def check(args):
        path, subscription_id, tenant_id, client_id, environment = args
        assert path == "azure-openai"
        assert subscription_id == "00000000-0000-0000-0000-000000000000"
        assert tenant_id == "11111111-1111-1111-1111-111111111111"
        assert client_id == "22222222-2222-2222-2222-222222222222"
        assert environment == "AzurePublicCloud"

    return pulumi.Output.all(
        engine.azure_secrets_engine.path,
        engine.azure_secrets_engine.subscription_id,
        engine.azure_secrets_engine.tenant_id,
        engine.azure_secrets_engine.client_id,
        engine.azure_secrets_engine.environment,
    ).apply(check)


@pulumi.runtime.test
def test_mount_lease_ttls_are_integer_seconds():
    """Mount-level TTLs really are ints, unlike the role-level ones."""
    engine = OLVaultAzureSecretsEngine(
        _engine_config(default_lease_ttl_seconds=3600, max_lease_ttl_seconds=7200)
    )

    def check(args):
        default_ttl, max_ttl = args
        assert default_ttl == 3600
        assert max_ttl == 7200

    return pulumi.Output.all(
        engine.azure_secrets_engine.default_lease_ttl_seconds,
        engine.azure_secrets_engine.max_lease_ttl_seconds,
    ).apply(check)


# ─── Rendered roles ───────────────────────────────────────────────────────────


@pulumi.runtime.test
def test_role_is_named_via_role_argument_not_name():
    """Regression guard for trap 1 -- ``role=``, not ``name=``.

    Passing ``name=`` instead would leave this as an auto-generated string
    rather than the role name callers actually read credentials from.
    """
    engine = OLVaultAzureSecretsEngine(_engine_config())

    def check(role):
        assert role == "ol-mitlearn-openai"

    return engine.azure_secrets_engine_roles["ol-mitlearn-openai"].role.apply(check)


@pulumi.runtime.test
def test_role_ttls_render_as_duration_strings():
    """Regression guard for trap 2 -- "24h"/"48h", never 86400/172800."""
    engine = OLVaultAzureSecretsEngine(
        _engine_config(
            roles={
                "ol-mitlearn-openai": OLVaultAzureRoleConfig(
                    role_name="Cognitive Services OpenAI User",
                    scope=COGNITIVE_ACCOUNT_SCOPE,
                    ttl="8h",
                    max_ttl="12h",
                )
            }
        )
    )
    role_resource = engine.azure_secrets_engine_roles["ol-mitlearn-openai"]

    def check(args):
        ttl, max_ttl = args
        assert ttl == "8h"
        assert max_ttl == "12h"

    return pulumi.Output.all(role_resource.ttl, role_resource.max_ttl).apply(check)


@pulumi.runtime.test
def test_role_azure_role_carries_role_name_and_scope():
    """The scope is what confines a consumer to its own OpenAI account."""
    engine = OLVaultAzureSecretsEngine(_engine_config())

    def check(azure_roles):
        assert len(azure_roles) == 1
        assert azure_roles[0]["role_name"] == "Cognitive Services OpenAI User"
        assert azure_roles[0]["scope"] == COGNITIVE_ACCOUNT_SCOPE

    return engine.azure_secrets_engine_roles["ol-mitlearn-openai"].azure_roles.apply(
        check
    )


@pulumi.runtime.test
def test_each_configured_role_gets_its_own_scope():
    """Per-consumer scoping is the isolation boundary, so verify it per role."""
    mitxonline_scope = COGNITIVE_ACCOUNT_SCOPE.replace("mitlearn", "mitxonline")
    engine = OLVaultAzureSecretsEngine(
        _engine_config(
            roles={
                "ol-mitlearn-openai": OLVaultAzureRoleConfig(
                    role_name="Cognitive Services OpenAI User",
                    scope=COGNITIVE_ACCOUNT_SCOPE,
                ),
                "ol-mitxonline-openai": OLVaultAzureRoleConfig(
                    role_name="Cognitive Services OpenAI User",
                    scope=mitxonline_scope,
                ),
            }
        )
    )
    assert set(engine.azure_secrets_engine_roles) == {
        "ol-mitlearn-openai",
        "ol-mitxonline-openai",
    }

    def check(args):
        mitlearn_roles, mitxonline_roles = args
        assert mitlearn_roles[0]["scope"] == COGNITIVE_ACCOUNT_SCOPE
        assert mitxonline_roles[0]["scope"] == mitxonline_scope

    return pulumi.Output.all(
        engine.azure_secrets_engine_roles["ol-mitlearn-openai"].azure_roles,
        engine.azure_secrets_engine_roles["ol-mitxonline-openai"].azure_roles,
    ).apply(check)
