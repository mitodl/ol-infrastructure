"""In-cluster Redis backing the vMCP embedded auth server's persistent storage.

The VirtualMCPServer's embedded OAuth authorization server keeps its OAuth
sessions and DCR client registrations here (``authServerConfig.storage.redis``)
so they survive vMCP pod restarts. Without it these live in memory and are wiped
on restart, so DCR clients get ``invalid_client`` and must re-register.

A small single-replica StatefulSet with a PVC, local to the stack's namespace.
The CRD requires a password (``aclUserConfig.passwordSecretRef``) for the Redis
backend, so Redis runs with requirepass and the vMCP references the same secret.
"""

from typing import NamedTuple

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions

from ol_infrastructure.lib.aws.eks_helper import cached_image_uri
from ol_infrastructure.lib.pulumi_helper import StackInfo

REDIS_SERVICE_NAME = "toolhive-swe-redis"
REDIS_PASSWORD_SECRET_NAME = "toolhive-swe-redis-password"  # noqa: S105  # pragma: allowlist secret
REDIS_PASSWORD_SECRET_KEY = "password"  # noqa: S105  # pragma: allowlist secret
REDIS_IMAGE = "redis:7.4-alpine"
REDIS_STORAGE_CLASS = "ebs-gp3-sc"


def redis_addr(namespace: str) -> str:
    """Return the in-cluster address the vMCP uses to reach this Redis."""
    return f"{REDIS_SERVICE_NAME}.{namespace}.svc.cluster.local:6379"


class ToolhiveSWERedis(NamedTuple):
    """Handles to the provisioned Redis resources for depends_on wiring."""

    password_secret: kubernetes.core.v1.Secret
    service: kubernetes.core.v1.Service
    statefulset: kubernetes.apps.v1.StatefulSet


def create_redis_resources(
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    toolhive_swe_config: Config,
) -> ToolhiveSWERedis:
    """Provision the Redis password Secret, headless Service, and StatefulSet."""
    # Redis requirepass from encrypted stack config. Generate + set (per environment):
    #   openssl rand -base64 32 \
    #     | pulumi config set --secret toolhive_swe:redis_password --
    redis_password_value = toolhive_swe_config.require_secret("redis_password")
    redis_password_secret = kubernetes.core.v1.Secret(
        f"toolhive-swe-redis-password-secret-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=REDIS_PASSWORD_SECRET_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        type="Opaque",
        string_data={REDIS_PASSWORD_SECRET_KEY: redis_password_value},
        opts=ResourceOptions(delete_before_replace=True),
    )

    # Headless Service: governs the StatefulSet and is the address the vMCP
    # connects to.
    redis_service = kubernetes.core.v1.Service(
        f"toolhive-swe-redis-service-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=REDIS_SERVICE_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            cluster_ip="None",
            selector={"app.kubernetes.io/name": REDIS_SERVICE_NAME},
            ports=[
                kubernetes.core.v1.ServicePortArgs(
                    name="redis", port=6379, target_port=6379, protocol="TCP"
                )
            ],
        ),
    )

    # Single-replica Redis with a small persistent volume. AOF persistence is
    # enabled so sessions/registrations survive Redis restarts too, and requirepass
    # is fed from the generated Secret. ``$(REDIS_PASSWORD)`` is substituted by
    # Kubernetes from the env.
    redis_pod_labels = {
        **k8s_global_labels,
        "app.kubernetes.io/name": REDIS_SERVICE_NAME,
    }
    redis_statefulset = kubernetes.apps.v1.StatefulSet(
        f"toolhive-swe-redis-statefulset-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=REDIS_SERVICE_NAME,
            namespace=namespace,
            labels=redis_pod_labels,
        ),
        spec=kubernetes.apps.v1.StatefulSetSpecArgs(
            service_name=REDIS_SERVICE_NAME,
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={"app.kubernetes.io/name": REDIS_SERVICE_NAME}
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=redis_pod_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="redis",
                            image=cached_image_uri(REDIS_IMAGE),
                            args=[
                                "redis-server",
                                "--requirepass",
                                "$(REDIS_PASSWORD)",
                                "--appendonly",
                                "yes",
                                "--dir",
                                "/data",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="REDIS_PASSWORD",
                                    value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                        secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                            name=REDIS_PASSWORD_SECRET_NAME,
                                            key=REDIS_PASSWORD_SECRET_KEY,
                                        )
                                    ),
                                )
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    container_port=6379
                                )
                            ],
                            readiness_probe=kubernetes.core.v1.ProbeArgs(
                                tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                    port=6379
                                ),
                                initial_delay_seconds=5,
                                period_seconds=10,
                            ),
                            liveness_probe=kubernetes.core.v1.ProbeArgs(
                                tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                    port=6379
                                ),
                                initial_delay_seconds=15,
                                period_seconds=20,
                            ),
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "50m", "memory": "64Mi"},
                                limits={"cpu": "200m", "memory": "256Mi"},
                            ),
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="data", mount_path="/data"
                                )
                            ],
                        )
                    ],
                ),
            ),
            volume_claim_templates=[
                kubernetes.core.v1.PersistentVolumeClaimArgs(
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        name="data", labels=k8s_global_labels
                    ),
                    spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
                        access_modes=["ReadWriteOnce"],
                        storage_class_name=REDIS_STORAGE_CLASS,
                        resources=kubernetes.core.v1.VolumeResourceRequirementsArgs(
                            requests={"storage": "100Gi"}
                        ),
                    ),
                )
            ],
        ),
        opts=ResourceOptions(depends_on=[redis_password_secret]),
    )

    return ToolhiveSWERedis(
        password_secret=redis_password_secret,
        service=redis_service,
        statefulset=redis_statefulset,
    )
