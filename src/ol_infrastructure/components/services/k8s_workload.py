"""
Component resource that encapsulates common patterns for IAM role binding
and Vault integration with Kubernetes workloads.

This component creates the authentication and authorization scaffolding
that Kubernetes workloads need, including:
- IAM Trust Role creation for EKS service accounts
- Kubernetes ServiceAccount creation with proper annotations
- Vault Secrets Operator integration (VaultConnection, VaultAuth)
- Static and dynamic secret management via Vault

Note: This component creates the auth/authz scaffolding, not the workload itself.
"""

from typing import Any, Literal

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pulumi import ComponentResource, Output, ResourceOptions
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.fields import Field

from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SSecretConfig,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase


class OLK8sAuthScaffoldVaultSecretConfig(BaseModel):
    """Configuration for a single Vault secret to be exposed to the workload."""

    secret_name: str
    vault_path: str
    vault_mount: str | Output[str]
    secret_type: Literal["static", "dynamic"] = "static"  # noqa: S105
    templates: dict[str, str | Output[str]] | None = None
    refresh_after: str | None = "1h"  # For static secrets
    renewal_percent: int | None = 67  # For dynamic secrets
    contents: dict[str, Any] | None = None  # For storing static secrets

    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLK8sAuthScaffoldIAMConfig(BaseModel):
    """Configuration for IAM permissions for the workload."""

    policy_statements: list[dict[str, Any]]
    role_description: str = "IAM role for Kubernetes workload"
    policy_operator: Literal["StringEquals", "StringLike"] = "StringEquals"

    model_config = ConfigDict(arbitrary_types_allowed=True)


class OLK8sAuthScaffoldConfig(AWSBase):
    """Configuration for Kubernetes auth scaffold with IAM and Vault integration."""

    # Basic workload configuration
    workload_name: str
    namespace: str
    cluster_name: str | Output[str]
    cluster_identities: Output[list[Any]]
    account_id: str | int

    # Vault configuration
    vault_address: str
    vault_auth_endpoint: str | Output[str]
    vault_auth_role_name: str | Output[str]
    vault_secrets: list[OLK8sAuthScaffoldVaultSecretConfig] = Field(
        default_factory=list
    )

    # IAM configuration
    iam_config: OLK8sAuthScaffoldIAMConfig | None = None

    # Kubernetes configuration
    labels: dict[str, str] | None = None
    annotations: dict[str, str] | None = None
    create_service_account: bool = True
    service_account_name: str | None = None

    model_config = ConfigDict(arbitrary_types_allowed=True)

    @field_validator("cluster_identities")
    @classmethod
    def validate_cluster_identities(cls, cluster_identities: Output[list[Any]]):
        """Ensure that the cluster identities are unwrapped from the Pulumi Output."""
        return Output.from_input(cluster_identities)


class OLK8sAuthScaffold(ComponentResource):
    """
    Component resource that encapsulates IAM role binding and Vault integration
    patterns for Kubernetes workloads.

    This component creates the authentication and authorization scaffolding:
    - IAM Trust Role with OIDC integration for EKS
    - IAM policies attached to the role
    - Kubernetes ServiceAccount with proper annotations
    - Vault Secrets Operator resources (VaultConnection, VaultAuth)
    - Static and dynamic Vault secrets exposed as Kubernetes secrets
    """

    def __init__(
        self,
        name: str,
        config: OLK8sAuthScaffoldConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure:k8s:OLK8sAuthScaffold",
            name,
            None,
            opts,
        )

        resource_opts = ResourceOptions(parent=self).merge(opts)

        # Set default service account name if not provided
        service_account_name = (
            config.service_account_name or f"{config.workload_name}-sa"
        )

        # Create IAM Trust Role if IAM configuration is provided
        if config.iam_config:
            trust_role_config = OLEKSTrustRoleConfig(
                account_id=config.account_id,
                cluster_name=config.cluster_name,
                cluster_identities=config.cluster_identities,
                description=config.iam_config.role_description,
                policy_operator=config.iam_config.policy_operator,
                role_name=f"{config.workload_name}-role",
                service_account_name=service_account_name,
                service_account_namespace=config.namespace,
                tags=config.tags,
            )

            self.trust_role = OLEKSTrustRole(
                f"{config.workload_name}-trust-role",
                role_config=trust_role_config,
                opts=resource_opts,
            )

            # Create and attach IAM policies
            self.iam_policies = []
            for i, policy_statement in enumerate(config.iam_config.policy_statements):
                policy_doc = {
                    "Version": "2012-10-17",
                    "Statement": [policy_statement]
                    if isinstance(policy_statement, dict)
                    else policy_statement,
                }

                policy = aws.iam.Policy(
                    f"{config.workload_name}-policy-{i}",
                    name=f"{config.workload_name}-policy-{i}",
                    path=f"/ol-infrastructure/k8s/{config.workload_name}/",
                    policy=pulumi.Output.json_dumps(policy_doc),
                    description=f"Policy {i} for {config.workload_name} workload",
                    tags=config.tags,
                    opts=resource_opts,
                )

                aws.iam.RolePolicyAttachment(
                    f"{config.workload_name}-policy-attachment-{i}",
                    role=self.trust_role.role.name,
                    policy_arn=policy.arn,
                    opts=resource_opts,
                )

                self.iam_policies.append(policy)
        else:
            self.trust_role = None
            self.iam_policies = []

        # Create Kubernetes ServiceAccount if requested
        if config.create_service_account:
            sa_annotations = config.annotations.copy() if config.annotations else {}

            # Add IAM role annotation if trust role was created
            if self.trust_role:
                # For service accounts, we need to handle the Output correctly
                iam_role_annotation = self.trust_role.role.arn.apply(
                    lambda arn: {**sa_annotations, "eks.amazonaws.com/role-arn": arn}
                )
            else:
                iam_role_annotation = sa_annotations

            self.service_account = kubernetes.core.v1.ServiceAccount(
                f"{config.workload_name}-service-account",
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    name=service_account_name,
                    namespace=config.namespace,
                    labels=config.labels,
                    annotations=iam_role_annotation,
                ),
                opts=resource_opts,
            )
        else:
            self.service_account = None

        # Create Vault K8s Resources
        vault_resources_config = OLVaultK8SResourcesConfig(
            application_name=config.workload_name,
            namespace=config.namespace,
            labels=config.labels,
            annotations=config.annotations,
            vault_address=config.vault_address,
            vault_auth_endpoint=config.vault_auth_endpoint,
            vault_auth_role_name=config.vault_auth_role_name,
        )

        self.vault_k8s_resources = OLVaultK8SResources(
            resource_config=vault_resources_config,
            opts=resource_opts,
        )

        # Create Vault secrets
        self.vault_secrets = []
        for secret_config in config.vault_secrets:
            if secret_config.secret_type == "static":  # noqa: S105
                vault_secret_config: OLVaultK8SSecretConfig = (
                    OLVaultK8SStaticSecretConfig(
                        name=f"{config.workload_name}-{secret_config.secret_name}",
                        dest_secret_name=secret_config.secret_name,
                        dest_secret_labels=config.labels,
                        labels=config.labels,
                        annotations=config.annotations,
                        mount=secret_config.vault_mount,
                        namespace=config.namespace,
                        path=secret_config.vault_path,
                        templates=secret_config.templates,
                        refresh_after=secret_config.refresh_after,
                        vaultauth=self.vault_k8s_resources.auth_name,
                        contents=secret_config.contents,
                    )
                )
            elif secret_config.secret_type == "dynamic":  # noqa: S105
                vault_secret_config: OLVaultK8SSecretConfig = (  # type: ignore[no-redef]
                    OLVaultK8SDynamicSecretConfig(
                        name=f"{config.workload_name}-{secret_config.secret_name}",
                        dest_secret_name=secret_config.secret_name,
                        dest_secret_labels=config.labels,
                        labels=config.labels,
                        annotations=config.annotations,
                        mount=secret_config.vault_mount,
                        namespace=config.namespace,
                        path=secret_config.vault_path,
                        templates=secret_config.templates,
                        vaultauth=self.vault_k8s_resources.auth_name,
                    )
                )
            else:
                msg = (
                    f"Invalid secret_type '{secret_config.secret_type}'."
                    " Must be 'static' or 'dynamic'."
                )
                raise ValueError(msg)

            vault_secret = OLVaultK8SSecret(
                f"{config.workload_name}-{secret_config.secret_name}-secret",
                resource_config=vault_secret_config,
                opts=ResourceOptions(
                    parent=self, depends_on=[self.vault_k8s_resources]
                ).merge(opts),
            )

            self.vault_secrets.append(vault_secret)

    @property
    def service_account_name(self) -> Output[str] | None:
        """Returns the name of the created service account."""
        if self.service_account:
            return self.service_account.metadata.name
        return None

    @property
    def iam_role_arn(self) -> Output[str] | None:
        """Returns the ARN of the created IAM role."""
        if self.trust_role:
            return self.trust_role.role.arn
        return None

    @property
    def vault_auth_name(self) -> str:
        """Returns the name of the Vault auth resource."""
        return self.vault_k8s_resources.auth_name

    @property
    def secret_names(self) -> list[str]:
        """Returns the names of all created Kubernetes secrets."""
        return [secret_config.secret_name for secret_config in self.vault_secrets]


# Convenience function for common patterns
def create_web_app_workload(  # noqa: PLR0913
    name: str,
    namespace: str,
    cluster_name: str | Output[str],
    cluster_identities: Output,
    vault_address: str,
    vault_auth_endpoint: str | Output[str],
    vault_auth_role_name: str | Output[str],
    account_id: str | int,
    region: str = "us-east-1",
    tags: dict[str, str] | None = None,
    s3_bucket_arn: str | None = None,
    database_mount: str | None = None,
    database_role: str | None = None,
    static_secrets_path: str | None = None,
    opts: ResourceOptions | None = None,
) -> OLK8sAuthScaffold:
    """Create a typical web application workload
    - S3 access permissions
    - Dynamic database credentials
    - Static application secrets
    """

    # Build IAM policy statements
    policy_statements = []

    # S3 access if bucket ARN provided
    if s3_bucket_arn:
        policy_statements.append(
            {
                "Effect": "Allow",
                "Action": [
                    "s3:GetObject",
                    "s3:PutObject",
                    "s3:DeleteObject",
                    "s3:ListBucket",
                ],
                "Resource": [s3_bucket_arn, f"{s3_bucket_arn}/*"],
            }
        )

    # Parameter Store access
    policy_statements.append(
        {
            "Effect": "Allow",
            "Action": [
                "ssm:GetParameter",
                "ssm:GetParameters",
                "ssm:GetParametersByPath",
            ],
            "Resource": [f"arn:aws:ssm:{region}:{account_id}:parameter/{name}/*"],
        }
    )

    # Build Vault secrets configuration
    vault_secrets = []

    # Database credentials if configured
    if database_mount and database_role:
        vault_secrets.append(
            OLK8sAuthScaffoldVaultSecretConfig(
                secret_name="database-credentials",  # noqa: S106
                vault_path=f"creds/{database_role}",
                vault_mount=database_mount,
                secret_type="dynamic",  # noqa: S106
                templates={
                    "DATABASE_URL": "postgresql://{{ .Secrets.username }}:{{ .Secrets.password }}@{{ .Secrets.host }}:{{ .Secrets.port }}/{{ .Secrets.database }}"  # noqa: E501
                },
            )
        )

    # Static application secrets if configured
    if static_secrets_path:
        vault_secrets.append(
            OLK8sAuthScaffoldVaultSecretConfig(
                secret_name="app-secrets",  # noqa: S106
                vault_path=static_secrets_path,
                vault_mount="secret-kv",
                secret_type="static",  # noqa: S106
            )
        )

    # Build configuration
    config = OLK8sAuthScaffoldConfig(
        workload_name=name,
        namespace=namespace,
        cluster_name=cluster_name,
        cluster_identities=cluster_identities,
        vault_address=vault_address,
        vault_auth_endpoint=vault_auth_endpoint,
        vault_auth_role_name=vault_auth_role_name,
        vault_secrets=vault_secrets,
        iam_config=OLK8sAuthScaffoldIAMConfig(
            policy_statements=policy_statements,
            role_description=f"IAM role for {name} web application",
        )
        if policy_statements
        else None,
        account_id=account_id,
        region=region,
        tags=tags or {},
    )

    return OLK8sAuthScaffold(name, config, opts)
