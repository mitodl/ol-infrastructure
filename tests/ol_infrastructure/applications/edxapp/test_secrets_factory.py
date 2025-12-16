"""Unit tests for secrets_factory module.

Tests the factory functions and builders that reduce boilerplate
in secret creation.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from ol_infrastructure.applications.edxapp.secrets_factory import (
    SecretRegistry,
    VaultSecretBuilder,
)
from ol_infrastructure.components.services.vault import OLVaultK8SResources
from ol_infrastructure.lib.pulumi_helper import StackInfo

# Ensure event loop exists for Python 3.14+ compatibility with Pulumi
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


@pytest.fixture
def mock_stack_info() -> StackInfo:
    """Create mock StackInfo."""
    return StackInfo(
        name="infrastructure.aws.edxapp.mitx",
        namespace="infrastructure.aws.edxapp",
        env_prefix="mitx",
        env_suffix="qa",
        full_name="infrastructure.aws.edxapp.mitx",
    )


@pytest.fixture
def mock_vault_resources() -> OLVaultK8SResources:
    """Create mock Vault K8S resources."""
    mock = MagicMock(spec=OLVaultK8SResources)
    mock.auth_name = "vault-auth"
    return mock


@pytest.fixture
def vault_builder(mock_stack_info, mock_vault_resources) -> VaultSecretBuilder:
    """Create VaultSecretBuilder instance."""
    return VaultSecretBuilder(
        stack_info=mock_stack_info,
        namespace="edxapp",
        k8s_global_labels={"environment": "qa", "app": "edxapp"},
        vault_k8s_resources=mock_vault_resources,
    )


class TestVaultSecretBuilder:
    """Test VaultSecretBuilder factory functions."""

    def test_builder_initialization(self, vault_builder, mock_stack_info):
        """Test builder initialization."""
        assert vault_builder.stack_info == mock_stack_info
        assert vault_builder.namespace == "edxapp"
        assert vault_builder.k8s_global_labels["app"] == "edxapp"

    def test_get_resource_name(self, vault_builder):
        """Test resource name generation."""
        name = vault_builder._get_resource_name()
        assert "mitx" in name
        assert "qa" in name

    def test_get_common_options(self, vault_builder):
        """Test common options generation."""
        opts = vault_builder._get_common_options()
        assert opts.delete_before_replace is True
        assert opts.depends_on is not None

    def test_create_static_secret(self, vault_builder):
        """Test creating a static secret."""
        secret = vault_builder.create_static(
            name="test-secret",
            secret_name="test-secret-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="test",
            templates={"test.yaml": "TEST_KEY: value"},
        )

        assert secret is not None
        # Check that it's an OLVaultK8SSecret (mocked resource)

    def test_create_static_with_env_prefix_placeholder(self, vault_builder):
        """Test mount expansion with {env_prefix} placeholder."""
        secret = vault_builder.create_static(
            name="test-secret",
            secret_name="test-secret-yaml",  # pragma: allowlist secret
            mount="secret-{env_prefix}",
            path="test",
            templates={"test.yaml": "TEST: value"},
        )

        assert secret is not None
        # Mount should be expanded to secret-mitx

    def test_create_static_with_custom_mount_type(self, vault_builder):
        """Test creating static secret with custom mount type."""
        secret = vault_builder.create_static(
            name="test-secret",
            secret_name="test-secret-yaml",  # pragma: allowlist secret
            mount="secret-global",
            path="test",
            templates={"test.yaml": "TEST: value"},
            mount_type="kv-v2",
        )

        assert secret is not None

    def test_create_dynamic_returns_callable(self, vault_builder):
        """Test create_dynamic returns a callable."""
        factory = vault_builder.create_dynamic(
            name="dynamic-secret",
            secret_name="dynamic-secret-yaml",  # pragma: allowlist secret
            mount="secret-{env_prefix}",
            path="test",
            templates={"test.yaml": 'TEST: {{ get .Secrets "value" }}'},
        )

        assert callable(factory)

    def test_create_dynamic_callable_creates_secret(self, vault_builder):
        """Test that dynamic callable can create secret with outputs."""
        factory = vault_builder.create_dynamic(
            name="dynamic-secret",
            secret_name="dynamic-secret-yaml",  # pragma: allowlist secret
            mount="secret-{env_prefix}",
            path="test",
            templates={"test.yaml": 'TEST: {{ get .Secrets "value" }}'},
        )

        # Call with dummy outputs dict
        secret = factory({})
        assert secret is not None


class TestSecretRegistry:
    """Test SecretRegistry for managing secrets."""

    def test_registry_initialization(self, vault_builder):
        """Test registry initialization."""
        registry = SecretRegistry(vault_builder)
        assert registry.builder == vault_builder
        assert len(registry.all()) == 0

    def test_add_static_secret(self, vault_builder):
        """Test adding a static secret to registry."""
        registry = SecretRegistry(vault_builder)

        registry.add_static(
            key="test-secret",
            name="test",
            secret_name="test-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="test",
            templates={"test.yaml": "TEST: value"},
        )

        assert "test-secret" in registry.all()
        assert registry.get_name("test-secret") == "test-yaml"

    def test_add_multiple_secrets(self, vault_builder):
        """Test adding multiple secrets to registry."""
        registry = SecretRegistry(vault_builder)

        for i in range(3):
            registry.add_static(
                key=f"secret-{i}",
                name=f"secret-{i}",
                secret_name=f"secret-{i}-yaml",
                mount="secret-mitx",
                path=f"test-{i}",
                templates={f"test-{i}.yaml": f"KEY_{i}: value_{i}"},
            )

        assert len(registry.all()) == 3
        assert len(registry.all_names()) == 3

    def test_add_conditional_secret_true(self, vault_builder):
        """Test adding conditional secret when condition is True."""
        registry = SecretRegistry(vault_builder)

        registry.add_conditional(
            key="optional-secret",
            name="optional",
            secret_name="optional-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="optional",
            templates={"optional.yaml": "OPTIONAL: value"},
            condition=True,
        )

        assert "optional-secret" in registry.all()

    def test_add_conditional_secret_false(self, vault_builder):
        """Test adding conditional secret when condition is False."""
        registry = SecretRegistry(vault_builder)

        registry.add_conditional(
            key="optional-secret",
            name="optional",
            secret_name="optional-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="optional",
            templates={"optional.yaml": "OPTIONAL: value"},
            condition=False,
        )

        assert "optional-secret" not in registry.all()

    def test_get_specific_secret(self, vault_builder):
        """Test getting a specific secret by key."""
        registry = SecretRegistry(vault_builder)

        registry.add_static(
            key="named-secret",
            name="named",
            secret_name="named-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="named",
            templates={"named.yaml": "NAMED: value"},
        )

        secret = registry.get("named-secret")
        assert secret is not None

    def test_get_nonexistent_secret(self, vault_builder):
        """Test getting nonexistent secret returns None."""
        registry = SecretRegistry(vault_builder)
        assert registry.get("nonexistent") is None

    def test_get_secret_name(self, vault_builder):
        """Test getting secret name by key."""
        registry = SecretRegistry(vault_builder)

        registry.add_static(
            key="named-secret",
            name="named",
            secret_name="my-secret-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="named",
            templates={"named.yaml": "NAMED: value"},
        )

        name = registry.get_name("named-secret")
        assert name == "my-secret-yaml"

    def test_get_nonexistent_secret_name(self, vault_builder):
        """Test getting nonexistent secret name returns None."""
        registry = SecretRegistry(vault_builder)
        assert registry.get_name("nonexistent") is None

    def test_registry_with_custom_mount_types(self, vault_builder):
        """Test registry with different mount types."""
        registry = SecretRegistry(vault_builder)

        # kv-v1 (default)
        registry.add_static(
            key="kv1-secret",
            name="kv1",
            secret_name="kv1-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="test",
            templates={"test.yaml": "KV1: value"},
            mount_type="kv-v1",
        )

        # kv-v2
        registry.add_static(
            key="kv2-secret",
            name="kv2",
            secret_name="kv2-yaml",  # pragma: allowlist secret
            mount="secret-global",
            path="test",
            templates={"test.yaml": "KV2: value"},
            mount_type="kv-v2",
        )

        assert len(registry.all()) == 2

    def test_registry_all_and_all_names(self, vault_builder):
        """Test getting all secrets and all names together."""
        registry = SecretRegistry(vault_builder)

        registry.add_static(
            key="secret-1",
            name="secret-1",
            secret_name="secret-1-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="test-1",
            templates={"test-1.yaml": "S1: value"},
        )

        registry.add_static(
            key="secret-2",
            name="secret-2",
            secret_name="secret-2-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="test-2",
            templates={"test-2.yaml": "S2: value"},
        )

        all_secrets = registry.all()
        all_names = registry.all_names()

        assert len(all_secrets) == 2
        assert len(all_names) == 2
        assert "secret-1" in all_names
        assert "secret-2" in all_names
        assert all_names["secret-1"] == "secret-1-yaml"
        assert all_names["secret-2"] == "secret-2-yaml"

    def test_registry_with_mixed_secret_types(self, vault_builder):
        """Test registry with both static and conditional secrets."""
        registry = SecretRegistry(vault_builder)

        # Always added
        registry.add_static(
            key="always",
            name="always",
            secret_name="always-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="always",
            templates={"always.yaml": "ALWAYS: value"},
        )

        # Conditionally added
        registry.add_conditional(
            key="sometimes",
            name="sometimes",
            secret_name="sometimes-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="sometimes",
            templates={"sometimes.yaml": "SOMETIMES: value"},
            condition=True,
        )

        # Never added
        registry.add_conditional(
            key="never",
            name="never",
            secret_name="never-yaml",  # pragma: allowlist secret
            mount="secret-mitx",
            path="never",
            templates={"never.yaml": "NEVER: value"},
            condition=False,
        )

        all_secrets = registry.all()
        assert len(all_secrets) == 2
        assert "always" in all_secrets
        assert "sometimes" in all_secrets
        assert "never" not in all_secrets
