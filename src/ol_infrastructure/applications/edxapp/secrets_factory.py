# ruff: noqa: E501, PLR0913, FBT001
# mypy: ignore-errors
"""Secret factory functions for edxapp Kubernetes secrets.

This module provides factory functions and builders to eliminate boilerplate
when creating OLVaultK8SSecret resources, reducing code from 500+ lines to ~200.
"""

from collections.abc import Callable
from typing import Any

from pulumi import Output, ResourceOptions

from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


class VaultSecretBuilder:
    """Factory for creating Vault Kubernetes secrets with reduced boilerplate.

    Eliminates ~20 lines of parameter passing for each secret by pre-setting
    common parameters (namespace, labels, dependencies, etc).

    Example:
        builder = VaultSecretBuilder(stack_info, namespace, labels, vault_resources)
        secret = builder.create_static(
            name="forum-secrets-yaml",
            mount="secret-mitx",
            path="edx-forum",
            template_file="forum.yaml",
            template_content="KEY: {{ get .Secrets \"value\" }}",
        )
    """

    def __init__(
        self,
        stack_info: StackInfo,
        namespace: str,
        k8s_global_labels: dict[str, str],
        vault_k8s_resources: OLVaultK8SResources,
    ):
        """Initialize secret builder with common parameters.

        Args:
            stack_info: Stack information (env_prefix, env_suffix)
            namespace: Kubernetes namespace for secrets
            k8s_global_labels: Labels to apply to all secrets
            vault_k8s_resources: Vault Kubernetes authentication resources
        """
        self.stack_info = stack_info
        self.namespace = namespace
        self.k8s_global_labels = k8s_global_labels
        self.vault_k8s_resources = vault_k8s_resources

    def get_resource_name(self, name: str) -> str:
        """Generate Pulumi resource name for secrets.

        Args:
            name: Resource name component (e.g., "db-creds-secret", "forum-secret")

        Returns:
            Full Pulumi resource name: ol-{env_prefix}-edxapp-{name}-{env_suffix}
        """
        return f"ol-{self.stack_info.env_prefix}-edxapp-{name}-{self.stack_info.env_suffix}"

    def get_common_options(self) -> ResourceOptions:
        """Generate common resource options."""
        return ResourceOptions(
            delete_before_replace=True,
            depends_on=[self.vault_k8s_resources],
        )

    def create_static(
        self,
        name: str,
        secret_name: str,
        mount: str,
        path: str,
        templates: dict[str, str],
        mount_type: str = "kv-v1",
        resource_name: str | None = None,
    ) -> OLVaultK8SSecret:
        """Create a static Vault secret (no Output.apply wrapper needed).

        Args:
            name: Resource name component for Pulumi resource identity
            secret_name: Kubernetes secret name
            mount: Vault mount point (can use {env_prefix} placeholder)
            path: Vault secret path
            templates: Dict of filename -> template content
            mount_type: Vault mount type (kv-v1 or kv-v2)
            resource_name: Optional override for Pulumi resource name (defaults to name)

        Returns:
            OLVaultK8SSecret resource
        """
        # Expand mount if it contains env_prefix placeholder
        mount = mount.replace("{env_prefix}", self.stack_info.env_prefix)

        return OLVaultK8SSecret(
            self.get_resource_name(resource_name or name),
            OLVaultK8SStaticSecretConfig(
                name=secret_name,
                namespace=self.namespace,
                dest_secret_labels=self.k8s_global_labels,
                dest_secret_name=secret_name,
                labels=self.k8s_global_labels,
                mount=mount,
                mount_type=mount_type,
                path=path,
                templates=templates,
                vaultauth=self.vault_k8s_resources.auth_name,
            ),
            opts=self.get_common_options(),
        )

    def create_dynamic(
        self,
        name: str,
        secret_name: str,
        mount: str,
        path: str,
        templates: dict[str, str],
    ) -> Callable[[dict[str, Any]], OLVaultK8SSecret]:
        """Create a factory function for dynamic Vault secrets with Output.apply.

        Returns a lambda that will be used with Output.apply() to handle
        dependencies on Output values (e.g., database addresses).

        Args:
            name: Resource name
            secret_name: Kubernetes secret name
            mount: Vault mount point (can use {env_prefix} placeholder)
            path: Vault secret path
            templates: Dict of filename -> template content

        Returns:
            Lambda function for use with Output.apply()

        Example:
            db_secret = Output.all(
                address=db.db_instance.address,
                port=db.db_instance.port,
            ).apply(
                builder.create_dynamic(
                    name="db-secret",
                    secret_name="db-yaml",  # pragma: allowlist secret
                    mount="mariadb-{env_prefix}",
                    path="creds/edxapp",
                    templates={"db.yaml": "..."},
                )
            )
        """
        # Expand mount if it contains env_prefix placeholder
        mount = mount.replace("{env_prefix}", self.stack_info.env_prefix)

        def _create_secret_with_outputs(_: Any) -> OLVaultK8SSecret:
            """Create secret with access to Output values."""
            return OLVaultK8SSecret(
                self.get_resource_name(f"dynamic-secret-{name}"),
                OLVaultK8SStaticSecretConfig(
                    name=secret_name,
                    namespace=self.namespace,
                    dest_secret_labels=self.k8s_global_labels,
                    dest_secret_name=secret_name,
                    labels=self.k8s_global_labels,
                    mount=mount,
                    mount_type="kv-v1",
                    path=path,
                    templates=templates,
                    vaultauth=self.vault_k8s_resources.auth_name,
                ),
                opts=self.get_common_options(),
            )

        return _create_secret_with_outputs


class SecretRegistry:
    """Registry for managing all edxapp secrets in a single place.

    Reduces main create_k8s_secrets() function to ~50 lines by centralizing
    secret registration and providing convenient methods to add secrets.

    Example:
        registry = SecretRegistry(builder)
        registry.add_static(
            key="forum",
            name="forum-yaml",
            secret_name="forum-secrets-yaml",  # pragma: allowlist secret
            mount="secret-{env_prefix}",
            path="edx-forum",
            templates={"forum.yaml": "..."},
        )
        all_secrets = registry.all()
        all_names = registry.all_names()
    """

    def __init__(self, builder: VaultSecretBuilder):
        """Initialize registry with secret builder.

        Args:
            builder: VaultSecretBuilder instance to use for creating secrets
        """
        self.builder = builder
        self._secrets: dict[str, OLVaultK8SSecret | Output] = {}
        self._names: dict[str, str] = {}

    def add_static(
        self,
        key: str,
        name: str,
        secret_name: str,
        mount: str,
        path: str,
        templates: dict[str, str],
        mount_type: str = "kv-v1",
    ) -> None:
        """Register a static Vault secret.

        Args:
            key: Internal registry key for this secret
            name: Resource name
            secret_name: Kubernetes secret name
            mount: Vault mount point
            path: Vault secret path
            templates: Dict of filename -> template content
            mount_type: Vault mount type (default: kv-v1)
        """
        secret = self.builder.create_static(
            name=name,
            secret_name=secret_name,
            mount=mount,
            path=path,
            templates=templates,
            mount_type=mount_type,
        )
        self._secrets[key] = secret
        self._names[key] = secret_name

    def add_dynamic(
        self,
        key: str,
        name: str,
        secret_name: str,
        mount: str,
        path: str,
        templates: dict[str, str],
        outputs: Output,
    ) -> None:
        """Register a dynamic Vault secret that depends on Output values.

        Args:
            key: Internal registry key for this secret
            name: Resource name
            secret_name: Kubernetes secret name
            mount: Vault mount point
            path: Vault secret path
            templates: Dict of filename -> template content
            outputs: Pulumi Output object to apply to
        """
        secret = outputs.apply(
            self.builder.create_dynamic(
                name=name,
                secret_name=secret_name,
                mount=mount,
                path=path,
                templates=templates,
            )
        )
        self._secrets[key] = secret
        self._names[key] = secret_name

    def add_conditional(
        self,
        key: str,
        name: str,
        secret_name: str,
        mount: str,
        path: str,
        templates: dict[str, str],
        condition: bool,
        mount_type: str = "kv-v1",
    ) -> None:
        """Register a conditional Vault secret (only if condition is True).

        Args:
            key: Internal registry key for this secret
            name: Resource name
            secret_name: Kubernetes secret name
            mount: Vault mount point
            path: Vault secret path
            templates: Dict of filename -> template content
            condition: Boolean condition to check
            mount_type: Vault mount type (default: kv-v1)
        """
        if condition:
            self.add_static(
                key=key,
                name=name,
                secret_name=secret_name,
                mount=mount,
                path=path,
                templates=templates,
                mount_type=mount_type,
            )

    def add_conditional_dynamic(
        self,
        key: str,
        name: str,
        secret_name: str,
        mount: str,
        path: str,
        templates: dict[str, str],
        outputs: Output,
        condition: bool,
    ) -> None:
        """Register a conditional dynamic secret (only if condition is True).

        Args:
            key: Internal registry key for this secret
            name: Resource name
            secret_name: Kubernetes secret name
            mount: Vault mount point
            path: Vault secret path
            templates: Dict of filename -> template content
            outputs: Pulumi Output object to apply to
            condition: Boolean condition to check
        """
        if condition:
            self.add_dynamic(
                key=key,
                name=name,
                secret_name=secret_name,
                mount=mount,
                path=path,
                templates=templates,
                outputs=outputs,
            )

    def all(self) -> dict[str, OLVaultK8SSecret | Output]:
        """Get all registered secrets.

        Returns:
            Dictionary of key -> OLVaultK8SSecret/Output
        """
        return self._secrets

    def all_names(self) -> dict[str, str]:
        """Get all registered secret names.

        Returns:
            Dictionary of key -> secret_name
        """
        return self._names

    def get(self, key: str) -> OLVaultK8SSecret | Output | None:
        """Get a specific secret by key.

        Args:
            key: Registry key for the secret

        Returns:
            Secret or None if not found
        """
        return self._secrets.get(key)

    def get_name(self, key: str) -> str | None:
        """Get a specific secret name by key.

        Args:
            key: Registry key for the secret

        Returns:
            Secret name or None if not found
        """
        return self._names.get(key)
