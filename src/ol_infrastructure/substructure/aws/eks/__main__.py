# ruff: noqa: E501

from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export

from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

env_config = Config("environment")
vault_config = Config("vault")

stack_info = parse_stack()

cluster_stack = StackReference(
    f"infrastructure.aws.eks.{stack_info.env_prefix}.{stack_info.name}"
)
cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={
        "OU": env_config.get("business_unit") or "operations",
        "Environment": cluster_name,
        "Owner": "platform-engineering",
    },
)

k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
}

setup_vault_provider(stack_info)
k8s_provider = kubernetes.Provider(
    "k8s-provider",
    kubeconfig=cluster_stack.require_output("kube_config"),
)

############################################################
# Secondary resources for vault-secrets-operator
############################################################

# install the *.odl.mit.edu certificate into the operations
# namespace with a VaultStaticSecret

# Setup a default auth backend role, used to load the *.odl.mit.edu certificate
# into the cluster. This shoudln't be used by applications. It is only for this
# one thing.  This restriction is enforced by binding to the `operations`
# namespace as well to a ServiceAccount within the operations namespace
vault_traefik_policy_name = f"{stack_info.env_prefix}-eks-traefik"
vault_traefik_service_account_name = "vault-traefik-auth"
vault_traefik_policy = vault.Policy(
    f"{cluster_name}-eks-vault-traefik-policy",
    name=vault_traefik_policy_name,
    policy=Path(__file__).parent.joinpath("traefik_vault_policy.hcl").read_text(),
)
vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"{cluster_name}-eks-vault-authentication-endpoint-operations",
    role_name="operations-default",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=[vault_traefik_service_account_name],
    bound_service_account_namespaces=["operations"],
    token_policies=[vault_traefik_policy_name],
)
# Create a k8s service account that will make the requests to vault
vault_traefik_service_account = kubernetes.core.v1.ServiceAccount(
    f"{cluster_name}-vault-traefik-service-account",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=vault_traefik_service_account_name,
        labels=k8s_global_labels,
        namespace="operations",
    ),
    automount_service_account_token=False,
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        delete_before_replace=True,
    ),
)

# We need to give the ServiceAccount the 'system:auth-delegator' ClusterRole
# which will allow vault to use the token that the request it makes to turn around
# and validate the request from the cluster api endpoint.
#
# Every application that wishes to use the vault-secrets-operator
# will need to implement this pattern because ServiceAccounts don't
# come with long-lived tokens starting with k8s 1.21
#
# This operational overhead is annoying but it is the best / most secure option
# for when working with a vault installation that resides outside of the cluster
#
# Ref: https://developer.hashicorp.com/vault/docs/auth/kubernetes#use-the-vault-client-s-jwt-as-the-reviewer-jwt
# Ref: https://developer.hashicorp.com/vault/docs/auth/kubernetes#configuring-kubernetes
vault_traefik_service_account_cluster_role_binding = (
    kubernetes.rbac.v1.ClusterRoleBinding(
        f"{cluster_name}-vault-traefik-service-account-cluster-role-binding",
        args=kubernetes.rbac.v1.ClusterRoleBindingInitArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=f"{vault_traefik_service_account_name}:cluster-auth",
                labels=k8s_global_labels,
                namespace="operations",
            ),
            role_ref=kubernetes.rbac.v1.RoleRefArgs(
                api_group="rbac.authorization.k8s.io",
                kind="ClusterRole",
                name="system:auth-delegator",
            ),
            subjects=[
                kubernetes.rbac.v1.SubjectArgs(
                    kind="ServiceAccount",
                    name=vault_traefik_service_account_name,
                    namespace="operations",
                ),
            ],
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=vault_traefik_service_account,
            delete_before_replace=True,
        ),
    )
)

# Create a VaultAuth resource and a VaultStaticSecret resource
# that will put 'secret-global/odl-wildcard'
# into a k8s Secret in the operations namespace named: "odl-wildcard-cert"
#
# Gateways in other namespaces will need ReferenceGrants
# added to the operations namespace in order to utilize it.
#
# Or they can setup their own VaultStaticSecret to import the
# certificate into their own namespace.
#
# They will need to create VaultConnection and VaultAuth
# resources as well
star_odl_mit_edu_secret_name = (
    "odl-wildcard-cert"  # pragma: allowlist secret #  noqa: S105
)
traefik_vso_resources = kubernetes.yaml.v2.ConfigGroup(
    f"{cluster_name}-traefik-vso-resources",
    objs=[
        {
            "apiVersion": "secrets.hashicorp.com/v1beta1",
            "kind": "VaultAuth",
            "metadata": {
                "name": "traefik-static-auth",
                "namespace": "operations",
                "labels": k8s_global_labels,
            },
            "spec": {
                "method": "kubernetes",
                "mount": cluster_stack.require_output("vault_auth_endpoint"),
                # This was for us by the helm chart
                "vaultConnectionRef": "default",
                "kubernetes": {
                    "role": vault_k8s_auth_backend_role.role_name,
                    "serviceAccount": vault_traefik_service_account_name,
                },
            },
        },
        {
            "apiVersion": "secrets.hashicorp.com/v1beta1",
            "kind": "VaultStaticSecret",
            "metadata": {
                "name": "vault-kv-global-odl-wildcard",
                "namespace": "operations",
                "labels": k8s_global_labels,
            },
            "spec": {
                "type": "kv-v2",
                "mount": "secret-global",
                "path": "odl-wildcard",
                "destination": {
                    "name": star_odl_mit_edu_secret_name,
                    "create": True,
                    "overwrite": True,
                    "type": "kubernetes.io/tls",
                    # Ref: https://developer.hashicorp.com/vault/docs/platform/k8s/vso/secret-transformation
                    "transformation": {
                        # Removes all the org fields from k8s secret
                        "excludes": [
                            ".*",
                        ],
                        # creates two new values in the k8s secret
                        # tls.key and tls.crt populated with data from the vault data
                        "templates": {
                            "tls.key": {
                                "text": '{{ get .Secrets "key_with_proper_newlines" }}',
                            },
                            "tls.crt": {
                                "text": '{{ get .Secrets "cert_with_proper_newlines" }}',
                            },
                        },
                    },
                },
                "refreshAfter": "1h",
                # This directly references the object above
                "vaultAuthRef": "traefik-static-auth",
            },
        },
    ],
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=vault_traefik_service_account,
        delete_before_replace=True,
    ),
)
export("star_odl_mit_edu_secret_name", star_odl_mit_edu_secret_name)
export("star_odl_mit_edu_secret_namespace", "operations")


############################################################
# Secondary resources for cert-manager
############################################################

# ClusterIssuer resources to provide a shared, preconfigured method
# for requesting certificates from letsencrypt
cert_manager_clusterissuer_resources = kubernetes.yaml.v2.ConfigGroup(
    f"{cluster_name}-cert-manager-clusterissuer-resources",
    skip_await=True,
    objs=[
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {
                "name": "letsencrypt-staging",
                "labels": k8s_global_labels,
            },
            "spec": {
                "acme": {
                    "email": "odl-devops@mit.edu",
                    "server": "https://acme-staging-v02.api.letsencrypt.org/directory",
                    "disableAccountKeyGeneration": True,
                    "privateKeySecretRef": {
                        "name": "letsencrypt-staging-private-key",
                    },
                    "solvers": [
                        {
                            "selector": {
                                "dnsZones": cluster_stack.require_output(
                                    "allowed_dns_zones"
                                ),
                            },
                            "dns01": {
                                "route53": {
                                    "region": aws_config.region,
                                    "role": cluster_stack.require_output(
                                        "cert_manager_arn"
                                    ),
                                },
                            },
                        },
                    ],
                },
            },
        },
        {
            "apiVersion": "cert-manager.io/v1",
            "kind": "ClusterIssuer",
            "metadata": {
                "name": "letsencrypt-production",
                "labels": k8s_global_labels,
            },
            "spec": {
                "acme": {
                    "email": "odl-devops@mit.edu",
                    "server": "https://acme-v02.api.letsencrypt.org/directory",
                    "disableAccountKeyGeneration": True,
                    "privateKeySecretRef": {
                        "name": "letsencrypt-production-private-key",
                    },
                    "solvers": [
                        {
                            "selector": {
                                "dnsZones": cluster_stack.require_output(
                                    "allowed_dns_zones"
                                ),
                            },
                            "dns01": {
                                "route53": {
                                    "region": aws_config.region,
                                    "role": cluster_stack.require_output(
                                        "cert_manager_arn"
                                    ),
                                },
                            },
                        },
                    ],
                },
            },
        },
    ],
    opts=ResourceOptions(
        provider=k8s_provider,
        parent=k8s_provider,
        delete_before_replace=True,
    ),
)
