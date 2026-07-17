"""The omnigraph data tier: a stateless ``omnigraph-server`` Deployment.

ADR-0009 (ol-infrastructure) decision point 2 / option 3: the data tier is a
plain Kubernetes ``Deployment`` (no PVC/StatefulSet — state lives entirely in
S3) reached by consumers (today, the ``witan`` MCPServer in the ``witan``
namespace) over the cluster network only, never exposed outside the cluster.
This mirrors the Redis-behind-vMCP precedent ``toolhive_swe`` uses for its own
stateful backend, except S3-backed so the pod itself needs no persistent
volume.

``omnigraph-server`` (an external Rust binary,
https://github.com/ModernRelay/omnigraph — not vendored in either repo) boots
from a ``--cluster <config-dir>`` pointing at a ``cluster.yaml`` (see
``docs/agent-memory.md`` "Remote Team Server" in agent-kit for the reference
CLI invocation this follows). That file is generated here as a ConfigMap
rather than hand-authored, so the S3 bucket name/region and graph list stay
in lockstep with the Pulumi-managed bucket.

No container image is built for ``omnigraph-server`` (or for witan) in either
repo yet — this stack only provisions the ECR repository and references
``:latest``, exactly like ``kubewatch_webhook_handler``'s pattern of "image
built separately, by a Concourse job, before this stack runs." That build
job does not exist yet; see the follow-up task noted in ``__main__.py``.
"""

import json
from typing import NamedTuple

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import yaml
from pulumi import Output, ResourceOptions

from ol_infrastructure.components.applications.eks import OLEKSAuthBinding
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.vault import OLVaultK8SSecret
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import StackInfo

OMNIGRAPH_SERVER_SERVICE_NAME = "omnigraph-server"
OMNIGRAPH_SERVER_PORT = 8080
OMNIGRAPH_SERVICE_ACCOUNT_NAME = "omnigraph-server"

# Mount path for the generated cluster.yaml ConfigMap (--cluster <config-dir>
# expects schema.pg alongside it — see CLUSTER_CONFIG_DIR below) and for the
# actor-tokens Secret (OMNIGRAPH_SERVER_BEARER_TOKENS_FILE), matching the
# WITAN_ACTOR_TOKENS_FILE mount witan itself uses in mcp_servers.py so both
# processes read the *same* generated artifact per agent-kit ADR-0004 D3.
CLUSTER_CONFIG_DIR = "/etc/omnigraph/cluster"
ACTOR_TOKENS_MOUNT_PATH = "/etc/omnigraph/actor-tokens"  # pragma: allowlist secret
ACTOR_TOKENS_FILENAME = "tokens.json"  # pragma: allowlist secret


def omnigraph_server_addr(namespace: str) -> str:
    """Return the in-cluster HTTP address witan's MCPServer talks to."""
    return (
        f"http://{OMNIGRAPH_SERVER_SERVICE_NAME}.{namespace}"
        f".svc.cluster.local:{OMNIGRAPH_SERVER_PORT}"
    )


class OmnigraphDataTier(NamedTuple):
    """Handles to the provisioned data-tier resources for depends_on wiring."""

    bucket: OLBucket
    ecr_repository: aws.ecr.Repository
    service: kubernetes.core.v1.Service
    deployment: kubernetes.apps.v1.Deployment


def create_data_tier(  # noqa: PLR0913
    stack_info: StackInfo,
    namespace: str,
    k8s_global_labels: dict[str, str],
    aws_config: AWSBase,
    auth_binding: OLEKSAuthBinding,
    actor_tokens_secret_name: str,
    actor_tokens_secret: OLVaultK8SSecret,
) -> OmnigraphDataTier:
    """Provision the S3 bucket, IRSA policy, ECR repo, ConfigMap, and Deployment."""
    # The bucket is named for its tenant (witan's graphs), not the omnigraph
    # service — omnigraph is generic and a future second instance would get its
    # own tenant-named bucket rather than colliding on an omnigraph-named one.
    bucket_name = f"ol-data-witan-{stack_info.env_suffix}"
    omnigraph_bucket = OLBucket(
        f"omnigraph-bucket-{stack_info.env_suffix}",
        S3BucketConfig(
            bucket_name=bucket_name,
            versioning_enabled=True,
            tags=aws_config.tags,
        ),
    )

    # The bucket ARN is only known as an Output, so the IAM policy is built
    # after the fact and attached to the IRSA role directly — the same
    # pattern clickhouse/__main__.py uses for its own OLBucket-backed IRSA
    # grant (iam_policy_document=None on the OLEKSAuthBinding config).
    omnigraph_s3_policy_json: Output[str] = Output.all(
        bucket_arn=omnigraph_bucket.bucket_v2.arn
    ).apply(
        lambda args: json.dumps(
            {
                "Version": IAM_POLICY_VERSION,
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "s3:GetObject",
                            "s3:PutObject",
                            "s3:DeleteObject",
                            "s3:ListBucket",
                            "s3:GetBucketLocation",
                            "s3:AbortMultipartUpload",
                            "s3:ListMultipartUploadParts",
                        ],
                        "Resource": [
                            args["bucket_arn"],
                            f"{args['bucket_arn']}/*",
                        ],
                    }
                ],
            }
        )
    )
    omnigraph_s3_iam_policy = aws.iam.Policy(
        f"omnigraph-s3-iam-policy-{stack_info.env_suffix}",
        policy=omnigraph_s3_policy_json,
    )
    aws.iam.RolePolicyAttachment(
        f"omnigraph-s3-iam-policy-attachment-{stack_info.env_suffix}",
        policy_arn=omnigraph_s3_iam_policy.arn,
        role=auth_binding.irsa_role.name,
    )

    # ECR repository for the omnigraph-server image. Built separately by a
    # Concourse job (not yet written — see __main__.py's follow-up note),
    # mirroring kubewatch_webhook_handler's "ECR repo here, image build
    # elsewhere" split.
    ecr_repository = aws.ecr.Repository(
        f"omnigraph-omnigraph-server-ecr-repository-{stack_info.env_suffix}",
        name=f"omnigraph-server-{stack_info.env_suffix.lower()}",
        image_tag_mutability="MUTABLE",
        image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
            scan_on_push=True,
        ),
        force_delete=True,
        tags=aws_config.tags,
    )
    aws.ecr.LifecyclePolicy(
        f"omnigraph-omnigraph-server-ecr-lifecycle-{stack_info.env_suffix}",
        repository=ecr_repository.name,
        policy=json.dumps(
            {
                "rules": [
                    {
                        "rulePriority": 1,
                        "description": "Keep last 10 images",
                        "selection": {
                            "tagStatus": "any",
                            "countType": "imageCountMoreThan",
                            "countNumber": 10,
                        },
                        "action": {"type": "expire"},
                    }
                ]
            }
        ),
    )
    omnigraph_server_image: Output[str] = ecr_repository.repository_url.apply(
        lambda url: f"{url}:latest"
    )

    # cluster.yaml — the single Layer-1 (memory/task/workflow) graph,
    # organization-wide, plus room for per-repo code graphs added the same
    # way (`omnigraph graphs` management, per ADR-0009 decision point 2).
    # `schema.pg` (agent-kit repo, mcp/servers/witan/schema/schema.pg) is NOT
    # sourced here — this Pulumi program has no access to agent-kit's working
    # tree at apply time. It must be baked into the omnigraph-server image at
    # build time, at ``{CLUSTER_CONFIG_DIR}/schema.pg``, by the (not yet
    # written) image-build job — see the follow-up note in __main__.py. The
    # ConfigMap volume below is mounted with ``sub_path`` so it overlays only
    # the single ``cluster.yaml`` file and leaves that baked-in schema.pg
    # visible alongside it, rather than replacing the whole directory.
    storage_uri: Output[str] = omnigraph_bucket.bucket_v2.bucket.apply(
        lambda name: f"s3://{name}"
    )
    cluster_name = f"mitodl-witan-{stack_info.env_suffix.lower()}"
    cluster_yaml_content: Output[str] = storage_uri.apply(
        lambda uri: yaml.dump(
            {
                "version": 1,
                "metadata": {"name": cluster_name},
                "state": {"backend": "cluster"},
                "storage": uri,
                "graphs": {"main": {"schema": "schema.pg"}},
            },
            sort_keys=False,
        )
    )
    cluster_configmap = kubernetes.core.v1.ConfigMap(
        f"omnigraph-omnigraph-cluster-config-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="omnigraph-cluster-config",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        data={"cluster.yaml": cluster_yaml_content},
    )

    omnigraph_pod_labels = {
        **k8s_global_labels,
        "app.kubernetes.io/name": OMNIGRAPH_SERVER_SERVICE_NAME,
    }
    omnigraph_deployment = kubernetes.apps.v1.Deployment(
        f"omnigraph-omnigraph-server-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=OMNIGRAPH_SERVER_SERVICE_NAME,
            namespace=namespace,
            labels=omnigraph_pod_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={"app.kubernetes.io/name": OMNIGRAPH_SERVER_SERVICE_NAME}
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=omnigraph_pod_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=OMNIGRAPH_SERVICE_ACCOUNT_NAME,
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="omnigraph-server",
                            image=omnigraph_server_image,
                            args=[
                                "--cluster",
                                CLUSTER_CONFIG_DIR,
                                "--bind",
                                f"0.0.0.0:{OMNIGRAPH_SERVER_PORT}",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="AWS_REGION", value=aws_config.region
                                ),
                                # Per-actor admission cap (tk-...-d241d2 sets
                                # real values for this deployment; the
                                # server's own defaults apply until then).
                                kubernetes.core.v1.EnvVarArgs(
                                    name="OMNIGRAPH_SERVER_BEARER_TOKENS_FILE",
                                    value=(
                                        f"{ACTOR_TOKENS_MOUNT_PATH}/"
                                        f"{ACTOR_TOKENS_FILENAME}"
                                    ),
                                ),
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    container_port=OMNIGRAPH_SERVER_PORT
                                )
                            ],
                            readiness_probe=kubernetes.core.v1.ProbeArgs(
                                tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                    port=OMNIGRAPH_SERVER_PORT
                                ),
                                initial_delay_seconds=5,
                                period_seconds=10,
                            ),
                            liveness_probe=kubernetes.core.v1.ProbeArgs(
                                tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                    port=OMNIGRAPH_SERVER_PORT
                                ),
                                initial_delay_seconds=15,
                                period_seconds=20,
                            ),
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "250m", "memory": "512Mi"},
                                limits={"cpu": "1", "memory": "2Gi"},
                            ),
                            volume_mounts=[
                                # sub_path overlays only cluster.yaml, leaving
                                # the image's baked-in schema.pg (same dir)
                                # visible — see the comment above the
                                # ConfigMap definition.
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="cluster-config",
                                    mount_path=f"{CLUSTER_CONFIG_DIR}/cluster.yaml",
                                    sub_path="cluster.yaml",
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="actor-tokens",
                                    mount_path=ACTOR_TOKENS_MOUNT_PATH,
                                    read_only=True,
                                ),
                            ],
                        )
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="cluster-config",
                            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                name=cluster_configmap.metadata.name,
                            ),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="actor-tokens",
                            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                                secret_name=actor_tokens_secret_name,
                            ),
                        ),
                    ],
                ),
            ),
        ),
        opts=ResourceOptions(
            depends_on=[
                cluster_configmap,
                actor_tokens_secret,
                # The pod's service_account_name is the IRSA SA this stack
                # creates via auth_binding (create_irsa_service_account=True);
                # wait for it so the initial apply doesn't transiently fail
                # with `serviceaccount "omnigraph-server" not found`.
                *auth_binding.irsa_service_accounts,
            ]
        ),
    )

    omnigraph_service = kubernetes.core.v1.Service(
        f"omnigraph-omnigraph-server-service-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=OMNIGRAPH_SERVER_SERVICE_NAME,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            selector={"app.kubernetes.io/name": OMNIGRAPH_SERVER_SERVICE_NAME},
            ports=[
                kubernetes.core.v1.ServicePortArgs(
                    name="http",
                    port=OMNIGRAPH_SERVER_PORT,
                    target_port=OMNIGRAPH_SERVER_PORT,
                    protocol="TCP",
                )
            ],
            type="ClusterIP",
        ),
        opts=ResourceOptions(depends_on=[omnigraph_deployment]),
    )

    return OmnigraphDataTier(
        bucket=omnigraph_bucket,
        ecr_repository=ecr_repository,
        service=omnigraph_service,
        deployment=omnigraph_deployment,
    )
