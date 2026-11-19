"""Tests for the Vault provider stack transformation in ol_infrastructure.lib.vault."""

from types import SimpleNamespace
from unittest.mock import Mock

import pulumi
import pulumi_vault
import pytest

from ol_infrastructure.lib import vault as vault_lib


@pytest.fixture
def stack_vault_provider(monkeypatch):
    """Stand in for the stack's own Vault provider without reading SOPS secrets."""
    provider = Mock(spec=pulumi_vault.Provider)
    monkeypatch.setattr(vault_lib, "get_vault_provider", lambda *_, **__: provider)
    return provider


def _transform(type_, provider=None):
    args = SimpleNamespace(
        type_=type_, props={}, opts=pulumi.ResourceOptions(provider=provider)
    )
    return vault_lib.set_vault_provider(
        "https://vault-qa.odl.mit.edu", "operations.qa", args
    )


def test_vault_resource_gets_stack_provider(stack_vault_provider):
    """A Vault resource with no provider of its own is sent to the stack's Vault"""
    result = _transform("vault:generic/secret:Secret")
    assert result.opts.provider is stack_vault_provider


@pytest.mark.usefixtures("stack_vault_provider")
def test_vault_resource_keeps_own_vault_provider():
    """A Vault resource given another environment's Vault provider keeps it"""
    other_vault = Mock(spec=pulumi_vault.Provider)
    result = _transform("vault:generic/secret:Secret", provider=other_vault)
    assert result.opts.provider is other_vault


@pytest.mark.usefixtures("stack_vault_provider")
def test_non_vault_resource_untouched():
    """Resources of other providers are left alone"""
    result = _transform("aws:s3/bucket:Bucket")
    assert result.opts.provider is None
