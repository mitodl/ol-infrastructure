# ruff: noqa: E501, F841, PLR0913
import base64
from pathlib import Path

import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import ResourceOptions, export
from pulumi_eks import Cluster

from ol_infrastructure.lib.pulumi_helper import StackInfo


def setup_vault_secrets_operator(
    cluster_name: str,
    cluster: Cluster,
    k8s_provider: kubernetes.Provider,
    operations_namespace: kubernetes.core.v1.Namespace,
    node_groups: list[eks.NodeGroupV2],
    stack_info: StackInfo,
    k8s_global_labels: dict[str, str],
    operations_tolerations: list[dict[str, str]],
    versions: dict[str, str],
):
    """
    Configure and install the vault-secrets-operator.

    :param cluster_name: The name of the EKS cluster.
    :param cluster: The EKS cluster object.
    :param k8s_provider: The Kubernetes provider for Pulumi.
    :param operations_namespace: The operations namespace object.
    :param node_groups: A list of EKS node groups.
    :param stack_info: Information about the current Pulumi stack.
    :param k8s_global_labels: A dictionary of global labels to apply to Kubernetes resources.
    :param operations_tolerations: A list of tolerations for scheduling on operations nodes.
    :param versions: A dictionary of component versions.
    """
    # Setup vault auth endpoint for the cluster.  Apps will need
    # their own auth backend roles added to auth backend which
    # we will export the name of below.
    vault_auth_endpoint_name = f"k8s-{stack_info.env_prefix}"
    vault_k8s_auth = vault.AuthBackend(
        f"{cluster_name}-eks-vault-k8s-auth-backend",
        type="kubernetes",
        path=vault_auth_endpoint_name,
        opts=ResourceOptions(
            parent=cluster, depends_on=cluster, delete_before_replace=True
        ),
    )
    vault.kubernetes.AuthBackendConfig(
        f"{cluster_name}-eks-vault-authentication-configuration-operations",
        kubernetes_ca_cert=cluster.eks_cluster.certificate_authority.data.apply(
            lambda b64_cert: "{}".format(base64.b64decode(b64_cert).decode("utf-8"))
        ),  # Important
        kubernetes_host=cluster.eks_cluster.endpoint,
        backend=vault_auth_endpoint_name,
        disable_iss_validation=True,  # Important
        disable_local_ca_jwt=False,  # Important
        opts=ResourceOptions(parent=vault_k8s_auth),
    )
    export("vault_auth_endpoint", vault_auth_endpoint_name)

    # This role allows the vault secrets operator to use a transit mount for
    # maintaining a cache of open leases. Makes operator restarts less painful
    # on applications
    # Ref: https://developer.hashicorp.com/vault/tutorials/kubernetes/vault-secrets-operator#transit-encryption
    transit_policy_name = f"{stack_info.env_prefix}-eks-vso-transit"
    transit_policy = vault.Policy(
        f"{cluster_name}-eks-vault-secrets-operator-transit-policy",
        name=transit_policy_name,
        policy=Path(__file__).parent.joinpath("vso_transit_policy.hcl").read_text(),
        opts=ResourceOptions(parent=vault_k8s_auth),
    )
    transit_role_name = "vso-transit"
    vault_secrets_operator_service_account_name = (
        "vault-secrets-operator-controller-manager"
    )
    vault_secret_operator_transit_role = vault.kubernetes.AuthBackendRole(
        f"{cluster_name}-eks-vault-secrets-operator-transit-role",
        role_name=transit_role_name,
        backend=vault_auth_endpoint_name,
        bound_service_account_names=[vault_secrets_operator_service_account_name],
        bound_service_account_namespaces=["operations"],
        token_policies=[transit_policy_name],
        opts=ResourceOptions(parent=vault_k8s_auth),
    )

    # Install the vault-secrets-operator directly from the public chart
    kubernetes.helm.v3.Release(
        f"{cluster_name}-vault-secrets-operator-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="vault-secrets-operator",
            chart="vault-secrets-operator",
            version=versions["VAULT_SECRETS_OPERATOR_CHART"],
            namespace="operations",
            cleanup_on_fail=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://helm.releases.hashicorp.com",
            ),
            values={
                "image": {
                    "pullPolicy": "Always",
                },
                "extraLabels": k8s_global_labels,
                "defaultVaultConnection": {
                    "enabled": True,
                    "address": f"https://vault-{stack_info.env_suffix}.odl.mit.edu",
                    "skipTLSVerify": False,
                },
                "controller": {
                    "replicas": 1,
                    "tolerations": operations_tolerations,
                    "manager": {
                        "resources": {
                            "requests": {
                                "memory": "64Mi",
                                "cpu": "10m",
                            },
                            "limits": {
                                "memory": "128Mi",
                                "cpu": "50m",
                            },
                        },
                        "clientCache": {
                            "persistenceModel": "direct-encrypted",
                            "storageEncryption": {
                                "enabled": True,
                                "mount": vault_auth_endpoint_name,
                                "keyName": "vault-secrets-operator",
                                "transitMount": "infrastructure",
                                "kubernetes": {
                                    "role": transit_role_name,
                                    "serviceAccount": "vault-secrets-operator-controller-manager",
                                    "tokenAudiences": [],
                                },
                            },
                        },
                    },
                },
            },
            skip_await=False,
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[cluster, node_groups[0], vault_secret_operator_transit_role],
            delete_before_replace=True,
        ),
    )
