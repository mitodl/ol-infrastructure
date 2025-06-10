# ruff: noqa: CPY001, D100
import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import BOTKUBE_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    Environment,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)

botkube_config = Config("config_botkube")
vault_config = Config("vault")
opensearch_stack = StackReference(
    f"infrastructure.aws.opensearch.apps.{stack_info.name}"
)
opensearch_cluster = opensearch_stack.require_output("cluster")
opensearch_cluster_endpoint = opensearch_cluster["endpoint"]

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")

aws_config = AWSBase(
    tags={"OU": BusinessUnit.operations, "Environment": Environment.operations},
)

botkube_namespace = "operations"

k8s_global_labels = K8sGlobalLabels(
    service=Services.botkube,
    ou=BusinessUnit.data,
    stack=stack_info,
).model_dump()

# Begin vault hoo-ha.
botkube_vault_secrets = read_yaml_secrets(
    Path(f"botkube/secrets.{stack_info.env_suffix}.yaml"),
)

botkube_vault_mount = vault.Mount(
    f"botkube-secrets-mount-{stack_info.env_suffix}",
    path="secret-botkube",
    type="kv-v2",
    options={"version": "2"},
    description="Secrets for the learn ai application.",
    opts=ResourceOptions(delete_before_replace=True),
)

botkube_static_vault_secrets = vault.generic.Secret(
    f"botkube-secrets-{stack_info.env_suffix}",
    path=botkube_vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps(botkube_vault_secrets),
)

botkube_vault_policy = vault.Policy(
    f"botkube-vault-policy-{stack_info.env_suffix}",
    name="botkube",
    policy=Path(__file__).parent.joinpath("botkube_policy.hcl").read_text(),
)

botkube_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"botkube-vault-auth-backend-role-{stack_info.env_suffix}",
    role_name="botkube",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[botkube_namespace],
    token_policies=[botkube_vault_policy.name],
)

# Stopped at: Make a kubernetes auth backend role that uses the policy we just installed
vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="botkube",
    namespace=botkube_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=botkube_vault_auth_backend_role.role_name,
)

vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[botkube_vault_auth_backend_role],
    ),
)

# Load the static secrets into a k8s secret via VSO
static_secrets_name = "communication-slack"  # pragma: allowlist secret
static_secrets = OLVaultK8SSecret(
    name=f"botkube-{stack_info.env_suffix}-static-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="botkube-static-secrets",
        namespace=botkube_namespace,
        labels=k8s_global_labels,
        dest_secret_name=static_secrets_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-botkube",
        mount_type="kv-v2",
        path="secrets",
        includes=["*"],
        excludes=[],
        exclude_raw=True,
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
        depends_on=[botkube_static_vault_secrets],
    ),
)
# end Vault hoo-ha


setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(botkube_namespace, ns)
)

# Install the botkube helm chart
botkube_application = kubernetes.helm.v3.Release(
    f"botkube-{stack_info.name}-application-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="botkube",
        chart="botkube",
        version=BOTKUBE_CHART_VERSION,
        namespace=botkube_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://charts.botkube.io",
        ),
        values={
            "commonLabels": k8s_global_labels,
            "communications": {
                "default-group": {
                    "socketSlack": {
                        "enabled": True,
                        "channels": {
                            "default": {
                                "name": "#botkube-ci",
                                "bindings": {
                                    "executors": ["k8s-default-tools"],
                                    "sources": [
                                        "k8s-err-events",
                                        "k8s-recommendation-events",
                                    ],
                                },
                            }
                        },
                    },
                },
            },
            "extraEnv": [
                {"name": "LOG_LEVEL_SOURCE_BOTKUBE_KUBERNETES", "value": "debug"},
                {
                    "name": (
                        "BOTKUBE_COMMUNICATIONS_DEFAULT-GROUP_SOCKET__SLACK_APP__TOKEN"
                    ),
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "communication-slack",
                            "key": "slack-app-token",
                        },
                    },
                },
                {
                    "name": (
                        "BOTKUBE_COMMUNICATIONS_DEFAULT-GROUP_SOCKET__SLACK_BOT__TOKEN"
                    ),
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "communication-slack",
                            "key": "slack-bot-token",
                        },
                    },
                },
            ],
        },
        skip_await=False,
    ),
)
