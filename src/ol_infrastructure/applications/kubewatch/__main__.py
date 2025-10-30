"""
Stack to build kubewatch service.

This service will echo select k8s events to Slack.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, StackReference, log

from bridge.lib.versions import KUBEWATCH_CHART_VERSION
from bridge.secrets.sops import read_yaml_secrets
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
kubewatch_sops_secrets = read_yaml_secrets(
    Path(f"kubewatch/secrets.{stack_info.env_prefix}.{stack_info.env_suffix}.yaml"),
)

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(kubewatch_namespace, ns)
)

slack_channel = Config("slack").require("channel_name")

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
            repo="https://robusta-charts.storage.googleapis.com",
        ),
        values={
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
            "slack": {
                "enabled": True,
                "channel": slack_channel,
                "token": kubewatch_sops_secrets["slack-token"],
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
            "extraEnv": [
                {
                    "name": "LOG_LEVEL",
                    "value": "debug",
                },
            ],
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
            "rbac": {
                "create": True,
                "customRoles": [
                    {
                        "apiGroups": ["events.k8s.io"],
                        "resources": ["events"],
                        "verbs": ["get", "list", "watch"],
                    }
                ],
            },
            "serviceAccount": {
                "create": True,
                "name": "",
                "automountServiceAccountToken": True,
                "annotations": {},
            },
        },
        skip_await=False,
    ),
)
