"""Tests for OLVaultK8SSecret's rendering of the VaultDynamicSecret spec.

Covers the revoke/renewalPercent fields added to OLVaultK8SDynamicSecretConfig:
explicit true/false must render verbatim, and unset must be omitted from the
spec entirely rather than rendered as null/false, since VSO's own zero-value
default differs from "field absent".
"""

from __future__ import annotations

import asyncio
from typing import Any

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


# Inputs of every mocked resource by name. The mock monitor keeps the secret
# wrapper on an input that was passed as a secret, so a test can check secretness.
REGISTERED_INPUTS: dict[str, dict[str, Any]] = {}


class K8sMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        REGISTERED_INPUTS[args.name] = args.inputs
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(K8sMocks())

import pytest  # noqa: E402
from pulumi.runtime.rpc import is_rpc_secret, unwrap_rpc_secret  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from ol_infrastructure.components.services import vault  # noqa: E402
from ol_infrastructure.components.services.vault import (  # noqa: E402
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)

# ─── Helpers ──────────────────────────────────────────────────────────────────


def _dynamic_config(**overrides) -> OLVaultK8SDynamicSecretConfig:
    defaults = {
        "dest_secret_name": "test-secret",  # pragma: allowlist secret
        "mount": "postgres-dagster",
        "name": "test-secret",
        "namespace": "test-ns",
        "path": "creds/app",
        "vaultauth": "test-auth",
    }
    defaults.update(overrides)
    return OLVaultK8SDynamicSecretConfig(**defaults)


def _rendered_spec(cfg):
    resource = OLVaultK8SSecret(f"test-{cfg.name}-{id(cfg)}", resource_config=cfg)
    return resource.vault_secret_resource.objs.apply(lambda objs: objs[0]["spec"])


# ─── revoke ────────────────────────────────────────────────────────────────


@pulumi.runtime.test
def test_revoke_true_rendered():
    def check(spec):
        assert spec["revoke"] is True

    return _rendered_spec(_dynamic_config(revoke_on_delete=True)).apply(check)


@pulumi.runtime.test
def test_revoke_false_rendered():
    """An explicit False must render, not be treated the same as unset."""

    def check(spec):
        assert spec["revoke"] is False

    return _rendered_spec(_dynamic_config(revoke_on_delete=False)).apply(check)


@pulumi.runtime.test
def test_revoke_omitted_when_unset():
    def check(spec):
        assert "revoke" not in spec

    return _rendered_spec(_dynamic_config()).apply(check)


# ─── renewalPercent ────────────────────────────────────────────────────────


@pulumi.runtime.test
def test_renewal_percent_rendered():
    def check(spec):
        assert spec["renewalPercent"] == 50

    return _rendered_spec(_dynamic_config(renewal_percent=50)).apply(check)


@pulumi.runtime.test
def test_renewal_percent_omitted_when_unset():
    def check(spec):
        assert "renewalPercent" not in spec

    return _rendered_spec(_dynamic_config()).apply(check)


@pulumi.runtime.test
def test_renewal_percent_zero_is_rendered():
    """0 is a valid, meaningful value -- must not be dropped as falsy."""

    def check(spec):
        assert spec["renewalPercent"] == 0

    return _rendered_spec(_dynamic_config(renewal_percent=0)).apply(check)


def test_renewal_percent_rejects_above_90():
    """VSO's CRD bounds renewalPercent to 0-90; mirror that in the model."""
    with pytest.raises(ValidationError):
        _dynamic_config(renewal_percent=91)


def test_renewal_percent_rejects_negative():
    with pytest.raises(ValidationError):
        _dynamic_config(renewal_percent=-1)


def test_renewal_percent_accepts_upper_bound():
    assert _dynamic_config(renewal_percent=90).renewal_percent == 90


# ─── static secrets are unaffected ────────────────────────────────────────


@pulumi.runtime.test
def test_static_secret_has_no_revoke_key():
    """revoke/renewalPercent are dynamic-secret-only; must not leak onto static."""
    cfg = OLVaultK8SStaticSecretConfig(
        dest_secret_name="test-static-secret",  # pragma: allowlist secret
        mount="secret-global",
        name="test-static-secret",
        namespace="test-ns",
        path="test-static-secret",
        vaultauth="test-auth",
    )

    def check(spec):
        assert "revoke" not in spec
        assert "renewalPercent" not in spec
        assert spec["refreshAfter"] == "1h"

    return _rendered_spec(cfg).apply(check)


# ─── database backend credentials ──────────────────────────────────────────


@pulumi.runtime.test
def test_database_backend_passes_admin_password_as_secret(monkeypatch):
    """The admin password reaches the connection as a secret, unchanged.

    The provider does not mark `data` sensitive, so an unwrapped password is
    stored in plaintext in the stack checkpoint.
    """

    class _CABundle:
        text = "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n"

    # Mocks are process-global and other test modules install their own, so
    # install the recording mocks here rather than relying on import order.
    pulumi.runtime.set_mocks(K8sMocks())
    monkeypatch.setattr(vault.httpx, "get", lambda _url: _CABundle())
    password = "test-admin-password"  # pragma: allowlist secret  # noqa: S105
    backend = OLVaultDatabaseBackend(
        OLVaultPostgresDatabaseConfig(
            db_name="secret-wrap-test",
            mount_point="postgres-secret-wrap-test",
            db_admin_username="admin",
            db_admin_password=password,
            db_host="db.example.com",
        )
    )

    def check(_urn):
        inputs = REGISTERED_INPUTS["secret-wrap-test-database-connection"]
        # A secret anywhere in a property marks the whole property secret.
        for block in (inputs["data"], inputs["postgresql"]):
            assert is_rpc_secret(block)
            assert unwrap_rpc_secret(block)["password"] == password

    return backend.db_connection.urn.apply(check)
