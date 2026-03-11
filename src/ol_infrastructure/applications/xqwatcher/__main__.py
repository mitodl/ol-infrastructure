"""Create the Kubernetes resources needed to run xqueue-watcher.  # noqa: D200

xqueue-watcher polls an xqueue server for student code submissions and grades
them by spawning an isolated container (ContainerGrader) per submission.  This
stack replaces the previous EC2 AMI-based deployment with a Kubernetes
Deployment on the shared applications EKS cluster.

Secrets are managed via the Vault Secrets Operator (VaultStaticSecret CRD).
"""

import json
import os
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import get_caller_identity

from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import AWSBase, K8sGlobalLabels, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

from bridge.secrets.sops import read_yaml_secrets

##################################
##    Setup + Config Retrieval  ##
##################################

if Config("vault_server").get("env_namespace") or Config("vault").get("address"):
    setup_vault_provider()

stack_info = parse_stack()
xqwatcher_config = Config("xqwatcher")

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
vault_mount_stack = StackReference(
    f"substructure.vault.static_mounts.operations.{stack_info.name}"
)

cluster_name = xqwatcher_config.get("cluster") or "applications"
cluster_stack = StackReference(
    f"infrastructure.aws.eks.{cluster_name}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

openedx_release = (
    OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)
    .release_by_env(stack_info.name)
    .value
)

aws_account = get_caller_identity()

aws_config = AWSBase(
    tags={
        "OU": xqwatcher_config.require("business_unit"),
        "Environment": env_name,
        "Application": "open-edx-xqwatcher",
        "Owner": "platform-engineering",
    }
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.xqwatcher,
    ou=xqwatcher_config.require("business_unit"),
    stack=stack_info,
)

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

namespace = xqwatcher_config.get("namespace") or f"{stack_info.env_prefix}-openedx"

docker_image_tag = (
    os.environ.get("XQWATCHER_DOCKER_DIGEST")
    or xqwatcher_config.get("docker_tag")
    or openedx_release
)

min_replicas = xqwatcher_config.get_int("min_replicas") or 1
max_replicas = xqwatcher_config.get_int("max_replicas") or 2

##################################
##      Vault Secret Data       ##
##################################

# Preserve management of the grader config secret in Vault KV.
# The VaultStaticSecret CRD (below) will sync this into the cluster.
vault_secrets = read_yaml_secrets(
    Path(f"xqwatcher/secrets.{stack_info.env_prefix}.{stack_info.env_suffix}.yaml")
)
xqwatcher_vault_mount_name = vault_mount_stack.require_output("xqwatcher_kv")["path"]
vault.kv.SecretV2(
    f"xqwatcher-{env_name}-grader-static-secrets",
    mount=xqwatcher_vault_mount_name,
    name=f"{stack_info.env_prefix}-grader-config",
    data_json=json.dumps(vault_secrets),
)

##################################
##    Vault Policy + K8s Auth   ##
##################################

vault_policy_template = (
    Path(__file__).parent.joinpath("xqwatcher_server_policy.hcl").read_text()
)
vault_policy_text = vault_policy_template.replace(
    "DEPLOYMENT", stack_info.env_prefix
)

xqwatcher_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name=f"xqwatcher-{stack_info.env_prefix}",
        namespace=namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,  # no direct AWS resource access required
        vault_policy_text=vault_policy_text,
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="xqwatcher",
        vault_sync_service_account_names=f"xqwatcher-{stack_info.env_prefix}-vault",
        k8s_labels=k8s_global_labels,
    )
)

vault_k8s_resources = xqwatcher_app.vault_k8s_resources

##################################
##        Vault Secrets         ##
##################################

# Grader handler config (queue names, ContainerGrader KWARGS, xqueue URL+auth).
# Stored as `confd_json` in the Vault KV entry written above.
grader_config_secret_name = "xqwatcher-grader-config"  # pragma: allowlist secret
grader_config_secret = OLVaultK8SSecret(
    f"xqwatcher-{env_name}-grader-config-secret",
    OLVaultK8SStaticSecretConfig(
        name=grader_config_secret_name,
        namespace=namespace,
        dest_secret_name=grader_config_secret_name,
        dest_secret_labels=k8s_global_labels.model_dump(),
        labels=k8s_global_labels.model_dump(),
        mount=xqwatcher_vault_mount_name,
        mount_type="kv-v2",
        path=f"{stack_info.env_prefix}-grader-config",
        refresh_after="1h",
        restart_target_kind="Deployment",
        restart_target_name="xqwatcher",
        # Expose just the rendered JSON as a file-friendly key.
        templates={
            "grader_config.json": "{{ .Secrets.confd_json }}",
        },
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[vault_k8s_resources],
    ),
)

##################################
##          ConfigMap           ##
##################################

# Base xqueue-watcher config (poll settings, logging).
# Per-queue grader config comes from the Vault-synced secret above.
xqwatcher_configmap = kubernetes.core.v1.ConfigMap(
    f"xqwatcher-{env_name}-configmap",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="xqwatcher-config",
        namespace=namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    data={
        "xqwatcher.json": json.dumps(
            {
                "FOLLOW_CLIENT_REDIRECTS": True,
                "POLL_INTERVAL": 10,
                "POLL_TIME": 10,
                "REQUESTS_TIMEOUT": 10,
            }
        ),
        # Emit logs to stdout only; no file rotation needed in containers.
        "logging.json": json.dumps(
            {
                "version": 1,
                "disable_existing_loggers": False,
                "formatters": {
                    "default": {
                        "format": "%(asctime)s - %(filename)s:%(lineno)d -- %(funcName)s [%(levelname)s]: %(message)s",
                    }
                },
                "handlers": {
                    "console": {
                        "class": "logging.StreamHandler",
                        "formatter": "default",
                        "level": "INFO",
                    }
                },
                "loggers": {
                    "": {
                        "handlers": ["console"],
                        "level": "INFO",
                    }
                },
            }
        ),
    },
)

##################################
##     RBAC for ContainerGrader ##
##################################

# xqwatcher uses the ContainerGrader backend which creates a Kubernetes Job
# per submission.  The service account running xqwatcher pods needs permission
# to create/delete Jobs and read pod logs in the same namespace.

xqwatcher_grader_role = kubernetes.rbac.v1.Role(
    f"xqwatcher-{env_name}-grader-role",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="xqwatcher-grader",
        namespace=namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    rules=[
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=["batch"],
            resources=["jobs"],
            verbs=["create", "delete", "get", "list", "watch"],
        ),
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=[""],
            resources=["pods", "pods/log"],
            verbs=["get", "list", "watch"],
        ),
    ],
)

xqwatcher_grader_rolebinding = kubernetes.rbac.v1.RoleBinding(
    f"xqwatcher-{env_name}-grader-rolebinding",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="xqwatcher-grader",
        namespace=namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    role_ref=kubernetes.rbac.v1.RoleRefArgs(
        api_group="rbac.authorization.k8s.io",
        kind="Role",
        name=xqwatcher_grader_role.metadata.name,
    ),
    subjects=[
        kubernetes.rbac.v1.SubjectArgs(
            kind="ServiceAccount",
            name="xqwatcher",
            namespace=namespace,
        ),
    ],
)

##################################
##         Deployment           ##
##################################

app_labels = {**k8s_global_labels.model_dump(), "app": "xqwatcher"}

xqwatcher_deployment = kubernetes.apps.v1.Deployment(
    f"xqwatcher-{env_name}-deployment",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="xqwatcher",
        namespace=namespace,
        labels=k8s_global_labels.model_dump(),
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=min_replicas,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": "xqwatcher"},
        ),
        strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
            type="RollingUpdate",
            rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                max_surge=1,
                max_unavailable=0,
            ),
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=app_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name="xqwatcher",
                # Spread replicas across nodes for HA
                topology_spread_constraints=[
                    kubernetes.core.v1.TopologySpreadConstraintArgs(
                        max_skew=1,
                        topology_key="kubernetes.io/hostname",
                        when_unsatisfiable="ScheduleAnyway",
                        label_selector=kubernetes.meta.v1.LabelSelectorArgs(
                            match_labels={"app": "xqwatcher"},
                        ),
                    )
                ],
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="xqueue-watcher",
                        image=f"ghcr.io/mitodl/xqueue-watcher:{docker_image_tag}",
                        image_pull_policy="IfNotPresent",
                        command=["xqueue-watcher"],
                        args=[
                            "--config", "/xqwatcher/conf.d/xqwatcher.json",
                            "--logging-config", "/xqwatcher/conf.d/logging.json",
                            "-d", "/xqwatcher/conf.d",
                        ],
                        # Liveness: verify the Python runtime is functional.
                        # The process will crash (and K8s will restart) on
                        # persistent xqueue connectivity failures, so we rely on
                        # the restart policy for connectivity-level health.
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            exec_=kubernetes.core.v1.ExecActionArgs(
                                command=[
                                    "python",
                                    "-c",
                                    "import xqueue_watcher; import sys; sys.exit(0)",
                                ]
                            ),
                            initial_delay_seconds=30,
                            period_seconds=60,
                            failure_threshold=3,
                            timeout_seconds=10,
                        ),
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "250m", "memory": "256Mi"},
                            limits={"memory": "512Mi"},
                        ),
                        security_context=kubernetes.core.v1.SecurityContextArgs(
                            allow_privilege_escalation=False,
                            run_as_non_root=True,
                            run_as_user=1000,
                            capabilities=kubernetes.core.v1.CapabilitiesArgs(
                                drop=["ALL"],
                            ),
                        ),
                        volume_mounts=[
                            # Base poll settings from ConfigMap
                            kubernetes.core.v1.VolumeMountArgs(
                                name="xqwatcher-config",
                                mount_path="/xqwatcher/conf.d/xqwatcher.json",
                                sub_path="xqwatcher.json",
                                read_only=True,
                            ),
                            kubernetes.core.v1.VolumeMountArgs(
                                name="xqwatcher-config",
                                mount_path="/xqwatcher/conf.d/logging.json",
                                sub_path="logging.json",
                                read_only=True,
                            ),
                            # Per-queue grader handler config from Vault secret
                            kubernetes.core.v1.VolumeMountArgs(
                                name="grader-config",
                                mount_path="/xqwatcher/conf.d/grader_config.json",
                                sub_path="grader_config.json",
                                read_only=True,
                            ),
                        ],
                    ),
                ],
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="xqwatcher-config",
                        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                            name=xqwatcher_configmap.metadata.name,
                        ),
                    ),
                    kubernetes.core.v1.VolumeArgs(
                        name="grader-config",
                        secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                            secret_name=grader_config_secret_name,
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(depends_on=[grader_config_secret]),
)

##################################
##           Exports            ##
##################################

export("k8s_deployment_name", "xqwatcher")
export("k8s_namespace", namespace)
export("grader_config_secret", grader_config_secret_name)
