"""Create the Kubernetes resources needed to run xqueue-watcher.  # noqa: D200

xqueue-watcher polls an xqueue server for student code submissions and grades
them by spawning an isolated container (ContainerGrader) per submission.  This
stack replaces the previous EC2 AMI-based deployment with a Kubernetes
Deployment on the shared applications EKS cluster.

Secrets are managed via the Vault Secrets Operator (VaultStaticSecret CRD).
"""

import copy
import json
import os
from pathlib import Path
from typing import Any

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference, export

from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import cached_image_uri, setup_k8s_provider
from ol_infrastructure.lib.ol_types import AWSBase, K8sGlobalLabels, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrieval  ##
##################################

if Config("vault_server").get("env_namespace") or Config("vault").get("address"):
    setup_vault_provider()

stack_info = parse_stack()
xqwatcher_config = Config("xqwatcher")

cluster_name = xqwatcher_config.get("cluster") or "applications"
cluster_stack = StackReference(
    f"infrastructure.aws.eks.{cluster_name}.{stack_info.name}"
)

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

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

if "XQWATCHER_DOCKER_DIGEST" not in os.environ:
    msg = "XQWATCHER_DOCKER_DIGEST must be set"
    raise ValueError(msg)
docker_image_digest = os.environ["XQWATCHER_DOCKER_DIGEST"]
docker_image_ref = f"mitodl/xqueue-watcher@{docker_image_digest}"

min_replicas = xqwatcher_config.get_int("min_replicas") or 1

# Deployment-wide ContainerGrader defaults.  These become XQWATCHER_GRADER_*
# environment variables on the xqwatcher pod so operators don't have to repeat
# them in every conf.d queue JSON file.  Per-queue KWARGS still override these.
grader_namespace = xqwatcher_config.get("grader_namespace") or namespace
grader_cpu_limit = xqwatcher_config.get("grader_cpu_limit") or "500m"
grader_memory_limit = xqwatcher_config.get("grader_memory_limit") or "256Mi"
grader_timeout = xqwatcher_config.get("grader_timeout") or "20"

##################################
##      Grader Queue Config     ##
##################################

xqueue_server_url = xqwatcher_config.require("xqueue_server_url")

# Read the non-secret queue configs from Pulumi stack config and inject
# SERVER_REF so credentials are resolved from xqueue_servers.json at runtime.
_queues_raw: dict[str, Any] = xqwatcher_config.require_object("queues")
queues_config: dict[str, Any] = {}
for queue_name, queue_cfg in _queues_raw.items():
    entry = copy.deepcopy(queue_cfg)
    # Rewrite bare DockerHub image refs to use the ECR pull-through cache.
    for handler_cfg in entry.get("HANDLERS", []):
        if handler_cfg.get("HANDLER", "").endswith(
            "ContainerGrader"
        ) and "image" in handler_cfg.get("KWARGS", {}):
            image_ref = handler_cfg["KWARGS"]["image"]
            first_component = image_ref.split("/", maxsplit=1)[0]
            if "." not in first_component and ":" not in first_component:
                handler_cfg["KWARGS"]["image"] = cached_image_uri(image_ref)
    entry["SERVER_REF"] = "default"
    queues_config[queue_name] = entry

##################################
##    Vault Policy + K8s Auth   ##
##################################

vault_policy_template = (
    Path(__file__).parent.joinpath("xqwatcher_server_policy.hcl").read_text()
)
vault_policy_text = vault_policy_template.replace("DEPLOYMENT", stack_info.env_prefix)

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
        create_irsa_service_account=True,
    )
)

vault_k8s_resources = xqwatcher_app.vault_k8s_resources

##################################
##        Vault Secrets         ##
##################################

# xqueue_servers.json — the only secret: xqueue URL and xqwatcher credentials.
# Sourced from the same Vault KV entry used by the xqueue and edxapp deployments.
xqueue_servers_secret_name = (
    "xqwatcher-xqueue-servers"  # pragma: allowlist secret  # noqa: S105
)
xqueue_servers_template = json.dumps(
    {
        "default": {
            "SERVER": xqueue_server_url,
            "AUTH": ["xqwatcher", "{{ .Secrets.xqwatcher_password }}"],
        }
    }
)
xqueue_servers_secret = OLVaultK8SSecret(
    f"xqwatcher-{env_name}-xqueue-servers-secret",
    OLVaultK8SStaticSecretConfig(
        name=xqueue_servers_secret_name,
        namespace=namespace,
        dest_secret_name=xqueue_servers_secret_name,
        dest_secret_labels=k8s_global_labels.model_dump(),
        labels=k8s_global_labels.model_dump(),
        mount=f"secret-{stack_info.env_prefix}",
        mount_type="kv-v1",
        path="edx-xqueue",
        refresh_after="1h",
        restart_target_kind="Deployment",
        restart_target_name="xqwatcher",
        templates={
            "xqueue_servers.json": xqueue_servers_template,
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

# Base xqueue-watcher config (poll settings, logging) and non-secret grader
# queue configs.  The Vault-synced secret provides xqueue_servers.json.
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
                        "format": "%(asctime)s - %(filename)s:%(lineno)d -- %(funcName)s [%(levelname)s]: %(message)s",  # noqa: E501
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
        # Non-secret queue configs; SERVER_REF resolves credentials at runtime
        # from xqueue_servers.json (mounted from the Vault-synced secret).
        "grader_config.json": json.dumps(queues_config),
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
                automount_service_account_token=True,
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
                        image=cached_image_uri(docker_image_ref),
                        image_pull_policy="Always",
                        command=["uv", "run", "--no-sync", "xqueue-watcher"],
                        args=["-d", "/xqwatcher"],
                        env=[
                            # Non-sensitive manager config values — match
                            # MANAGER_CONFIG_DEFAULTS in env_settings.py.
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_POLL_TIME", value="10"
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_REQUESTS_TIMEOUT", value="1"
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_POLL_INTERVAL", value="1"
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_LOGIN_POLL_INTERVAL", value="5"
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_FOLLOW_CLIENT_REDIRECTS",
                                value="true",
                            ),
                            # ContainerGrader deployment-wide defaults.
                            # These are used when a queue's KWARGS block does not
                            # specify the value explicitly.
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_GRADER_BACKEND",
                                value="kubernetes",
                            ),
                            # Critical: grading Jobs must land in the same
                            # namespace as xqwatcher so the RBAC Role binding
                            # above grants the necessary permissions.
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_GRADER_NAMESPACE",
                                value=grader_namespace,
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_GRADER_CPU_LIMIT",
                                value=grader_cpu_limit,
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_GRADER_MEMORY_LIMIT",
                                value=grader_memory_limit,
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="XQWATCHER_GRADER_TIMEOUT",
                                value=grader_timeout,
                            ),
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
                            # Manager config and logging config at the root of
                            # the -d directory; conf.d/ holds queue watcher configs.
                            kubernetes.core.v1.VolumeMountArgs(
                                name="xqwatcher-config",
                                mount_path="/xqwatcher/xqwatcher.json",
                                sub_path="xqwatcher.json",
                                read_only=True,
                            ),
                            kubernetes.core.v1.VolumeMountArgs(
                                name="xqwatcher-config",
                                mount_path="/xqwatcher/logging.json",
                                sub_path="logging.json",
                                read_only=True,
                            ),
                            # Per-queue grader handler config from the ConfigMap
                            # (non-secret: no SERVER/AUTH, uses SERVER_REF).
                            kubernetes.core.v1.VolumeMountArgs(
                                name="xqwatcher-config",
                                mount_path="/xqwatcher/conf.d/grader_config.json",
                                sub_path="grader_config.json",
                                read_only=True,
                            ),
                            # Named server definitions (SERVER URL + AUTH credentials)
                            # from the Vault-synced secret, mounted at the config
                            # root so xqueue-watcher can resolve SERVER_REF entries.
                            kubernetes.core.v1.VolumeMountArgs(
                                name="xqueue-servers",
                                mount_path="/xqwatcher/xqueue_servers.json",
                                sub_path="xqueue_servers.json",
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
                        name="xqueue-servers",
                        secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                            secret_name=xqueue_servers_secret_name,
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(depends_on=[xqueue_servers_secret]),
)

##################################
##           Exports            ##
##################################

export("k8s_deployment_name", "xqwatcher")
export("k8s_namespace", namespace)
export("xqueue_servers_secret", xqueue_servers_secret_name)
