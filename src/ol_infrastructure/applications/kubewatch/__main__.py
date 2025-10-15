"""
Stack to build kubewatch service.

This service will echo select k8s events to Slack.
"""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, log

from bridge.lib.versions import KUBEWATCH_CHART_VERSION
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
log.info(f"{stack_info=}")
setup_vault_provider(stack_info)

kubewatch_config = Config("config_kubewatch")
vault_config = Config("vault")

cluster_stack = StackReference(
    f"infrastructure.aws.eks.{stack_info.env_prefix}.{stack_info.name}"
)
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
aws_config = AWSBase(
    tags={"OU": BusinessUnit.operations, "Environment": Environment.operations},
)

kubewatch_namespace = "kubewatch"

k8s_global_labels = K8sGlobalLabels(
    service=Services.kubewatch,
    ou=BusinessUnit.operations,
    stack=stack_info,
).model_dump()

# Begin vault hoo-ha.
kubewatch_vault_secrets = read_yaml_secrets(
    Path(f"kubewatch/secrets.{stack_info.env_prefix}.{stack_info.env_suffix}.yaml"),
)

kubewatch_static_vault_secrets = vault.generic.Secret(
    f"kubewatch-secrets-operations-{stack_info.env_suffix}",
    path=f"secret-operations/kubewatch/{stack_info.env_prefix}",
    data_json=json.dumps(kubewatch_vault_secrets),
)

kubewatch_vault_policy = vault.Policy(
    f"kubewatch-vault-policy-{stack_info.env_suffix}",
    name="kubewatch",
    policy=Path(__file__).parent.joinpath("kubewatch_policy.hcl").read_text(),
)

kubewatch_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"kubewatch-vault-auth-backend-role-{stack_info.env_suffix}",
    role_name="kubewatch",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[kubewatch_namespace],
    token_policies=[kubewatch_vault_policy.name],
)

# Stopped at: Make a kubernetes auth backend role that uses the policy we just installed
vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="kubewatch",
    namespace=kubewatch_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=kubewatch_vault_auth_backend_role.role_name,
)

vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[kubewatch_vault_auth_backend_role],
    ),
)

# Load the static secrets into a k8s secret via VSO
static_secrets_name = "communication-slack"  # pragma: allowlist secret
static_secrets = OLVaultK8SSecret(
    name=f"kubewatch-{stack_info.env_suffix}-static-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="kubewatch-static-secrets",
        namespace=kubewatch_namespace,
        labels=k8s_global_labels,
        dest_secret_name=static_secrets_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path=f"kubewatch/{stack_info.env_prefix}",
        includes=["*"],
        excludes=[],
        exclude_raw=True,
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[kubewatch_static_vault_secrets],
    ),
)
# end Vault hoo-ha

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(kubewatch_namespace, ns)
)

slack_channel = Config("slack").require("channel_name")

log.info(f"Botkube Slack channel name: {slack_channel}")

# Install the kubewatch helm chart
kubewatch_application = kubernetes.helm.v3.Release(
    f"kubewatch-{stack_info.name}-application-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="kubewatch",
        chart="kubewatch",
        version=KUBEWATCH_CHART_VERSION,
        namespace=kubewatch_namespace,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://github.com/robusta-dev/kubewatch",
        ),
        values={
            "global": {"imageRegistry": "", "imagePullSecrets": []},
            "kubeVersion": "",
            "nameOverride": "",
            "fullnameOverride": "",
            "commonLabels": {},
            "commonAnnotations": {},
            "diagnosticMode": {
                "enabled": False,
                "command": ["sleep"],
                "args": ["infinity"],
            },
            "extraDeploy": [],
            "image": {
                "repository": "robustadev/kubewatch",
                "tag": "v2.9.0",
                "pullPolicy": "IfNotPresent",
                "pullSecrets": [],
            },
            "hostAliases": [],
            "slack": {"enabled": True, "channel": "XXXX", "token": "XXXX"},
            "slackwebhook": {
                "enabled": False,
                "channel": "XXXX",
                "username": "",
                "emoji": "",
                "slackwebhookurl": "XXXX",
            },
            "extraHandlers": {},
            "namespaceToWatch": "",
            "resourcesToWatch": {
                "deployment": True,
                "replicationcontroller": False,
                "replicaset": False,
                "daemonset": False,
                "services": False,
                "pod": True,
                "job": False,
                "persistentvolume": False,
                "event": True,
            },
            "customresources": [],
            "command": [],
            "args": [],
            "lifecycleHooks": {},
            "extraEnvVars": [],
            "extraEnvVarsCM": "",
            "extraEnvVarsSecret": "",
            "replicaCount": 1,
            "podSecurityContext": {"enabled": False, "fsGroup": ""},
            "containerSecurityContext": {
                "enabled": False,
                "runAsUser": "",
                "runAsNonRoot": "",
            },
            "resources": {"limits": {}, "requests": {}},
            "startupProbe": {
                "enabled": False,
                "initialDelaySeconds": 10,
                "periodSeconds": 10,
                "timeoutSeconds": 1,
                "failureThreshold": 3,
                "successThreshold": 1,
            },
            "livenessProbe": {
                "enabled": False,
                "initialDelaySeconds": 10,
                "periodSeconds": 10,
                "timeoutSeconds": 1,
                "failureThreshold": 3,
                "successThreshold": 1,
            },
            "readinessProbe": {
                "enabled": False,
                "initialDelaySeconds": 10,
                "periodSeconds": 10,
                "timeoutSeconds": 1,
                "failureThreshold": 3,
                "successThreshold": 1,
            },
            "customStartupProbe": {},
            "customLivenessProbe": {},
            "customReadinessProbe": {},
            "podAffinityPreset": "",
            "podAntiAffinityPreset": "soft",
            "nodeAffinityPreset": {"type": "", "key": "", "values": []},
            "affinity": {},
            "nodeSelector": {},
            "tolerations": [],
            "priorityClassName": "",
            "schedulerName": "",
            "topologySpreadConstraints": [],
            "podLabels": {},
            "podAnnotations": {},
            "extraVolumes": [],
            "extraVolumeMounts": [],
            "updateStrategy": {"type": "RollingUpdate"},
            "initContainers": [],
            "sidecars": [],
            "rbac": {"create": False, "customRoles": []},
            "serviceAccount": {
                "create": True,
                "name": "",
                "automountServiceAccountToken": True,
                "annotations": {},
            },
        },
    ),
    opts=ResourceOptions(
        depends_on=[static_secrets],
    ),
)
