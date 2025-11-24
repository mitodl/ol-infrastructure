"""Common components for deploying applications to EKS."""

from pathlib import Path
from typing import Any

from pulumi import ComponentResource, Config, Output, ResourceOptions
from pulumi_aws import get_caller_identity, iam
from pulumi_vault import Policy
from pulumi_vault import kubernetes as vault_kubernetes
from pydantic import BaseModel

from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase, K8sGlobalLabels
from ol_infrastructure.lib.pulumi_helper import StackInfo, parse_stack


class OLEKSAuthBindingConfig(BaseModel):
    """Configuration for EKS application services.

    This component is intended to simplify the deployment of applications on EKS that
    interact with AWS services and Vault. It will provision the required IAM and Vault
    policies and roles to allow the application to authenticate with both AWS (via IRSA)
    and Vault (via Kubernetes service account).
    """

    application_name: str
    namespace: str
    stack_info: StackInfo
    aws_config: AWSBase
    iam_policy_document: dict[str, Any]
    vault_policy_path: Path
    # From cluster stack reference
    cluster_identities: Output[Any]
    vault_auth_endpoint: Output[str]
    # Name(s) of the k8s service account(s) that will be used for IRSA
    # Can be a single string or a list of strings
    irsa_service_account_name: str | list[str]
    # Name(s) of the k8s service account(s) used for vault secret sync
    # Can be a single string or a list of strings
    vault_sync_service_account_names: str | list[str] = "vault-secrets"
    # Labels to apply to k8s resources created by the component
    k8s_labels: K8sGlobalLabels
    # Optional parliament config for IAM policy linting
    parliament_config: dict[str, Any] | None = None

    class Config:
        """Pydantic model configuration."""

        arbitrary_types_allowed = True


class OLEKSAuthBinding(ComponentResource):
    """A component for deploying applications to EKS."""

    irsa_role: iam.Role
    vault_k8s_resources: OLVaultK8SResources

    def __init__(
        self,
        config: OLEKSAuthBindingConfig,
        opts: ResourceOptions | None = None,
    ):
        """Initialize the EKS application component.

        :param config: The configuration for the application.
        :param opts: The Pulumi resource options.
        """
        super().__init__(
            "ol:infrastructure:aws:eks:OLEKSApplication",
            config.application_name,
            {},
            opts,
        )
        stack_info = parse_stack()
        aws_account = get_caller_identity()
        self.iam_policy = iam.Policy(
            f"{config.application_name}-policy-{config.stack_info.env_suffix}",
            name=f"{config.application_name}-policy-{config.stack_info.env_suffix}",
            path=f"/ol-data/{config.application_name}-policy-{config.stack_info.env_suffix}/",
            policy=lint_iam_policy(
                config.iam_policy_document,
                stringify=True,
                parliament_config=config.parliament_config,
            ),
            description=(
                f"Policy for granting access for {config.application_name} to AWS"
                " resources"
            ),
            opts=ResourceOptions(parent=self),
        )

        self.trust_role = OLEKSTrustRole(
            f"{config.application_name}-irsa-trust-role-{config.stack_info.env_suffix}",
            role_config=OLEKSTrustRoleConfig(
                account_id=aws_account.account_id,
                cluster_name=f"data-{config.stack_info.name}",
                cluster_identities=config.cluster_identities,
                description=(
                    f"Trust role for {config.application_name} k8s service account"
                ),
                policy_operator="StringEquals",
                role_name=config.application_name,
                service_account_name=config.irsa_service_account_name,
                service_account_namespace=config.namespace,
                tags=config.aws_config.tags,
            ),
            opts=ResourceOptions(parent=self),
        )

        iam.RolePolicyAttachment(
            f"{config.application_name}-irsa-policy-attach-{config.stack_info.env_suffix}",
            policy_arn=self.iam_policy.arn,
            role=self.trust_role.role.name,
            opts=ResourceOptions(parent=self),
        )
        self.irsa_role = self.trust_role.role

        vault_policy = Policy(
            f"{config.application_name}-server-vault-policy",
            name=f"{config.application_name}-server",
            policy=config.vault_policy_path.read_text(),
            opts=ResourceOptions(parent=self),
        )

        # Convert service account names to list if it's a single string
        service_account_names = (
            [config.vault_sync_service_account_names]
            if isinstance(config.vault_sync_service_account_names, str)
            else config.vault_sync_service_account_names
        )

        k8s_auth_backend_role = vault_kubernetes.AuthBackendRole(
            f"{config.application_name}-k8s-vault-auth-backend-role-{config.stack_info.env_suffix}",
            role_name=config.application_name,
            backend=config.vault_auth_endpoint,
            bound_service_account_names=service_account_names,
            bound_service_account_namespaces=[config.namespace],
            token_policies=[vault_policy.name],
            opts=ResourceOptions(parent=self),
        )

        vault_k8s_resources_config = OLVaultK8SResourcesConfig(
            application_name=config.application_name,
            namespace=config.namespace,
            labels=config.k8s_labels.model_dump(),
            vault_address=Config("vault").get("address")
            or f"https://vault-{stack_info.env_suffix}.odl.mit.edu",
            vault_auth_endpoint=config.vault_auth_endpoint,
            vault_auth_role_name=k8s_auth_backend_role.role_name,
            service_account_name=service_account_names[0],
        )
        self.vault_k8s_resources = OLVaultK8SResources(
            resource_config=vault_k8s_resources_config,
            opts=ResourceOptions(
                delete_before_replace=True,
                depends_on=[k8s_auth_backend_role],
                parent=self,
            ),
        )

        self.register_outputs(
            {
                "iam_policy": self.iam_policy,
                "irsa_role": self.irsa_role,
                "vault_policy": vault_policy,
                "vault_k8s_auth_role": k8s_auth_backend_role,
                "vault_k8s_resources": self.vault_k8s_resources,
            }
        )
