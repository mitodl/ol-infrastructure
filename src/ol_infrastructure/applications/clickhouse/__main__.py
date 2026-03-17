# ruff: noqa: E501
"""Deploy a multi-tenant ClickHouse cluster for LLMOps workloads on the data EKS cluster.

Architecture:
- Altinity ClickHouseInstallation CRD manages the cluster (installed by the
  substructure.aws.eks.data stack).
- ClickHouse Keeper (3 replicas) provides distributed coordination.
- Hot storage: local NVMe SSD via ``local-nvme`` when enabled by the data EKS
  stack; otherwise EBS gp3.
- Cold storage: S3 bucket with Intelligent-Tiering; ClickHouse moves data via
  table-level TTL MOVE expressions.
- Multi-tenancy: separate database + user + resource quota per LLMOps tool.
  Passwords are stored in Vault KV and synced to a K8s Secret as a users.xml
  file, which is volume-mounted into ClickHouse pods.

Requires the ``substructure.aws.eks.data.*`` stack to be deployed (which installs
the NVMe DaemonSet and local-path-provisioner) before deploying to QA/Production.
"""

import json
from pathlib import Path
from textwrap import dedent
from typing import Any

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, StackReference, export

from bridge.lib.versions import (
    CLICKHOUSE_KEEPER_VERSION,
    CLICKHOUSE_OPERATOR_VERSION,  # noqa: F401 — for traceability
    CLICKHOUSE_SERVER_VERSION,
)
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack, require_stack_output_value
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()

clickhouse_config = Config("clickhouse")
vault_config = Config("vault")
stack_info = parse_stack()

vault_mount_stack = StackReference(
    f"substructure.vault.static_mounts.operations.{stack_info.name}"
)
cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")
stateful_workload_storage = require_stack_output_value(
    cluster_stack, "stateful_workload_storage"
)

clickhouse_vault_kv_path = vault_mount_stack.require_output("clickhouse_kv")["path"]

clickhouse_env = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.data,
        "Environment": clickhouse_env,
        "Application": "clickhouse",
        "Owner": "platform-engineering",
    }
)

k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.full_name,
    "ol.mit.edu/stack": stack_info.full_name,
    "app.kubernetes.io/managed-by": "pulumi",
}

k8s_labels = K8sGlobalLabels(
    ou=BusinessUnit.data,
    service=Services.clickhouse,
    stack=stack_info,
)

setup_k8s_provider(kubeconfig=require_stack_output_value(cluster_stack, "kube_config"))
CLICKHOUSE_NAMESPACE = "clickhouse"

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(CLICKHOUSE_NAMESPACE, ns)
)

# Configuration values
hot_data_days = int(clickhouse_config.get("hot_data_days") or "7")
hot_storage_size = clickhouse_config.get("hot_storage_size") or "100Gi"
storage_class = stateful_workload_storage["storage_class"]
use_io_optimized_nodes = stateful_workload_storage["use_io_optimized_nodes"]
ch_replicas = int(clickhouse_config.get("replicas") or "1")
keeper_replicas = int(clickhouse_config.get("keeper_replicas") or "1")
ch_version = clickhouse_config.get("version") or CLICKHOUSE_SERVER_VERSION
ch_image = f"altinity/clickhouse-server:{ch_version}"
keeper_image = f"clickhouse/clickhouse-keeper:{CLICKHOUSE_KEEPER_VERSION}"

IO_OPTIMIZED_NODE_SELECTOR = {"ol.mit.edu/io_optimized": "true"}
IO_OPTIMIZED_TOLERATION = kubernetes.core.v1.TolerationArgs(
    key="ol.mit.edu/io-workload",
    operator="Equal",
    value="true",
    effect="NoSchedule",
)
IO_OPTIMIZED_NODE_AFFINITY = kubernetes.core.v1.AffinityArgs(
    node_affinity=kubernetes.core.v1.NodeAffinityArgs(
        required_during_scheduling_ignored_during_execution=kubernetes.core.v1.NodeSelectorArgs(
            node_selector_terms=[
                kubernetes.core.v1.NodeSelectorTermArgs(
                    match_expressions=[
                        kubernetes.core.v1.NodeSelectorRequirementArgs(
                            key="ol.mit.edu/io_optimized",
                            operator="In",
                            values=["true"],
                        )
                    ]
                )
            ]
        )
    )
)

_QUOTA_PROFILES_XML = dedent("""\
    <clickhouse>
      <profiles>
        <llmops_profile>
          <max_memory_usage>4000000000</max_memory_usage>
          <max_concurrent_queries_for_user>10</max_concurrent_queries_for_user>
          <readonly>0</readonly>
        </llmops_profile>
      </profiles>
      <quotas>
        <llmops_quota>
          <interval>
            <duration>3600</duration>
            <queries>1000</queries>
            <errors>100</errors>
            <result_rows>1000000000</result_rows>
            <read_rows>10000000000</read_rows>
            <execution_time>3600</execution_time>
          </interval>
        </llmops_quota>
      </quotas>
    </clickhouse>
""")


def _require_password(password_output: Output, username: str) -> Output:
    """Fail fast if a ClickHouse user password is left at the insecure default."""

    def _check(password: str) -> str:
        if password == "changeme":  # pragma: allowlist secret  # noqa: S105
            msg = (
                f"ClickHouse password for user '{username}' must be set via "
                f"'pulumi config set --secret clickhouse:{username}_password' "
                f"and cannot use the default 'changeme' value."  # pragma: allowlist secret
            )
            raise ValueError(msg)
        return password

    return password_output.apply(_check)


def _create_keeper_resources(  # noqa: PLR0913
    *,
    env_suffix: str,
    namespace: str,
    labels: dict[str, str],
    replicas: int,
    image: str,
    use_io_optimized: bool,
    ebs_storageclass_output: "Output[Any]",
    fallback_storage_class: str,
) -> kubernetes.apps.v1.StatefulSet:
    """Create ClickHouse Keeper resources and return the StatefulSet.

    Creates the ConfigMap, headless Service, client Service, and StatefulSet
    for ClickHouse Keeper.  The StatefulSet is returned because it is used as
    a ``depends_on`` target by the ClickHouseInstallation resource.
    """
    config_map = kubernetes.core.v1.ConfigMap(
        f"clickhouse-keeper-config-{env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="clickhouse-keeper-config",
            namespace=namespace,
            labels=labels,
        ),
        data={
            "keeper.xml": keeper_config_xml.format(
                raft_servers=_build_raft_servers(replicas)
            )
        },
    )

    headless_service = kubernetes.core.v1.Service(
        f"clickhouse-keeper-headless-{env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="clickhouse-keeper-headless",
            namespace=namespace,
            labels={**labels, "app": "clickhouse-keeper"},
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            cluster_ip="None",
            selector={"app": "clickhouse-keeper"},
            ports=[
                kubernetes.core.v1.ServicePortArgs(name="client", port=2181),
                kubernetes.core.v1.ServicePortArgs(name="raft", port=9444),
            ],
        ),
    )

    client_service = kubernetes.core.v1.Service(
        f"clickhouse-keeper-client-{env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="clickhouse-keeper",
            namespace=namespace,
            labels={**labels, "app": "clickhouse-keeper"},
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            selector={"app": "clickhouse-keeper"},
            ports=[
                kubernetes.core.v1.ServicePortArgs(name="client", port=2181),
            ],
        ),
    )

    tolerations = [IO_OPTIMIZED_TOLERATION] if use_io_optimized else []
    node_selector = IO_OPTIMIZED_NODE_SELECTOR if use_io_optimized else {}

    return kubernetes.apps.v1.StatefulSet(
        f"clickhouse-keeper-{env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="clickhouse-keeper",
            namespace=namespace,
            labels={**labels, "app": "clickhouse-keeper"},
        ),
        spec=kubernetes.apps.v1.StatefulSetSpecArgs(
            replicas=replicas,
            service_name="clickhouse-keeper-headless",
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={"app": "clickhouse-keeper"},
            ),
            pod_management_policy="Parallel",
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels={**labels, "app": "clickhouse-keeper"},
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    tolerations=tolerations,
                    node_selector=node_selector,
                    affinity=kubernetes.core.v1.AffinityArgs(
                        node_affinity=(
                            IO_OPTIMIZED_NODE_AFFINITY.node_affinity
                            if use_io_optimized
                            else None
                        ),
                        pod_anti_affinity=kubernetes.core.v1.PodAntiAffinityArgs(
                            required_during_scheduling_ignored_during_execution=[
                                kubernetes.core.v1.PodAffinityTermArgs(
                                    label_selector=kubernetes.meta.v1.LabelSelectorArgs(
                                        match_labels={"app": "clickhouse-keeper"},
                                    ),
                                    topology_key="kubernetes.io/hostname",
                                )
                            ]
                        ),
                    )
                    if replicas > 1
                    else None,
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="set-server-id",
                            image="busybox:stable-musl",
                            command=[
                                "sh",
                                "-c",
                                # Extract pod index from hostname (e.g. clickhouse-keeper-0 -> 1)
                                "export MY_ID=$((${HOSTNAME##*-} + 1)); "
                                "echo MY_ID=$MY_ID > /keeper-env/env",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="HOSTNAME",
                                    value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                        field_ref=kubernetes.core.v1.ObjectFieldSelectorArgs(
                                            field_path="metadata.name"
                                        )
                                    ),
                                )
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="keeper-env",
                                    mount_path="/keeper-env",
                                )
                            ],
                        )
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="clickhouse-keeper",
                            image=image,
                            command=[
                                "sh",
                                "-c",
                                ". /keeper-env/env && export MY_ID && /entrypoint.sh",
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    name="client", container_port=2181
                                ),
                                kubernetes.core.v1.ContainerPortArgs(
                                    name="raft", container_port=9444
                                ),
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "100m", "memory": "256Mi"},
                                limits={"memory": "512Mi"},
                            ),
                            liveness_probe=kubernetes.core.v1.ProbeArgs(
                                exec_=kubernetes.core.v1.ExecActionArgs(
                                    command=[
                                        "sh",
                                        "-c",
                                        "echo ruok | nc localhost 2181",
                                    ]
                                ),
                                initial_delay_seconds=30,
                                period_seconds=30,
                                failure_threshold=3,
                            ),
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="keeper-config",
                                    mount_path="/etc/clickhouse-keeper/keeper.xml",
                                    sub_path="keeper.xml",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="keeper-data",
                                    mount_path="/var/lib/clickhouse-keeper",
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="keeper-env",
                                    mount_path="/keeper-env",
                                ),
                            ],
                        )
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="keeper-config",
                            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                name="clickhouse-keeper-config",
                            ),
                        ),
                        kubernetes.core.v1.VolumeArgs(
                            name="keeper-env",
                            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
                        ),
                    ],
                ),
            ),
            volume_claim_templates=[
                kubernetes.core.v1.PersistentVolumeClaimArgs(
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        name="keeper-data",
                        labels=labels,
                    ),
                    spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
                        access_modes=["ReadWriteOnce"],
                        # Keeper stores only coordination logs; always prefer EBS for
                        # durability. Fall back to the cluster storage class if the EBS
                        # CSI provisioner is not enabled on this cluster.
                        storage_class_name=ebs_storageclass_output.apply(
                            lambda sc: sc if sc is not None else fallback_storage_class
                        ),
                        resources=kubernetes.core.v1.VolumeResourceRequirementsArgs(
                            requests={"storage": "20Gi"},
                        ),
                    ),
                )
            ],
        ),
        opts=ResourceOptions(depends_on=[config_map, headless_service, client_service]),
    )


def _create_clickhouse_installation(  # noqa: PLR0913
    *,
    env_suffix: str,
    namespace: str,
    labels: dict[str, str],
    ch_replicas: int,
    ch_image: str,
    use_io_optimized: bool,
    storage_class: str,
    hot_storage_size: str,
    cold_bucket_name: "Output[str]",
    users_secret_name: str,
    irsa_role_arn: "Output[str]",
    keeper_statefulset: "kubernetes.apps.v1.StatefulSet",
    users_secret: Any,
    vault_k8s_resources: Any,
) -> "Output[kubernetes.apiextensions.CustomResource]":
    """Build the ClickHouseInstallation CRD and return it wrapped in an Output.

    Uses ``Output.all().apply()`` because the storage configuration XML must be
    resolved from the cold-storage bucket name, which is an ``Output[str]``.
    """
    ch_tolerations = (
        [
            {
                "key": "ol.mit.edu/io-workload",
                "operator": "Equal",
                "value": "true",
                "effect": "NoSchedule",
            }
        ]
        if use_io_optimized
        else []
    )
    ch_node_selector = IO_OPTIMIZED_NODE_SELECTOR if use_io_optimized else {}
    ch_affinity = (
        {
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {
                            "matchLabels": {"clickhouse.altinity.com/chi": "clickhouse"}
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            },
            **(
                {
                    "nodeAffinity": {
                        "requiredDuringSchedulingIgnoredDuringExecution": {
                            "nodeSelectorTerms": [
                                {
                                    "matchExpressions": [
                                        {
                                            "key": "ol.mit.edu/io_optimized",
                                            "operator": "In",
                                            "values": ["true"],
                                        }
                                    ]
                                }
                            ]
                        }
                    }
                }
                if use_io_optimized
                else {}
            ),
        }
        if ch_replicas > 1
        else (
            {
                "nodeAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": {
                        "nodeSelectorTerms": [
                            {
                                "matchExpressions": [
                                    {
                                        "key": "ol.mit.edu/io_optimized",
                                        "operator": "In",
                                        "values": ["true"],
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
            if use_io_optimized
            else {}
        )
    )

    storage_config_xml = cold_bucket_name.apply(
        lambda bucket: dedent(f"""\
            <clickhouse>
              <storage_configuration>
                <disks>
                  <hot_local>
                    <type>local</type>
                    <path>/var/lib/clickhouse/</path>
                  </hot_local>
                  <cold_s3>
                    <type>s3</type>
                    <endpoint>https://{bucket}.s3.amazonaws.com/data/</endpoint>
                    <use_environment_credentials>true</use_environment_credentials>
                    <metadata_path>/var/lib/clickhouse/disks/cold_s3/</metadata_path>
                  </cold_s3>
                </disks>
                <policies>
                  <tiered>
                    <volumes>
                      <hot>
                        <disk>hot_local</disk>
                      </hot>
                      <cold>
                        <disk>cold_s3</disk>
                        <prefer_not_to_merge>true</prefer_not_to_merge>
                      </cold>
                    </volumes>
                  </tiered>
                </policies>
              </storage_configuration>
            </clickhouse>
        """)
    )

    return Output.all(
        storage_config=storage_config_xml,
        irsa_role_arn=irsa_role_arn,
    ).apply(
        lambda kwargs: kubernetes.apiextensions.CustomResource(
            f"clickhouse-installation-{env_suffix}",
            api_version="clickhouse.altinity.com/v1",
            kind="ClickHouseInstallation",
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name="clickhouse",
                namespace=namespace,
                labels=labels,
            ),
            spec={
                "defaults": {
                    "templates": {
                        "podTemplate": "clickhouse-pod-template",
                        "dataVolumeClaimTemplate": "clickhouse-data",
                    }
                },
                "configuration": {
                    "zookeeper": {
                        "nodes": [
                            {
                                "host": f"clickhouse-keeper.{namespace}.svc.cluster.local",
                                "port": 2181,
                            }
                        ]
                    },
                    "clusters": [
                        {
                            "name": "default",
                            "layout": {
                                "shardsCount": 1,
                                "replicasCount": ch_replicas,
                            },
                        }
                    ],
                    "files": {
                        "config.d/storage.xml": kwargs["storage_config"],
                        "config.d/quotas_profiles.xml": _QUOTA_PROFILES_XML,
                    },
                    "settings": {
                        "default_storage_policy": "tiered",
                    },
                },
                "templates": {
                    "podTemplates": [
                        {
                            "name": "clickhouse-pod-template",
                            "spec": {
                                "serviceAccountName": "clickhouse",
                                "tolerations": ch_tolerations,
                                "nodeSelector": ch_node_selector,
                                "affinity": ch_affinity,
                                "containers": [
                                    {
                                        "name": "clickhouse",
                                        "image": ch_image,
                                        "resources": {
                                            "requests": {
                                                "cpu": "500m",
                                                "memory": "4Gi",
                                            },
                                            "limits": {
                                                "memory": "8Gi",
                                            },
                                        },
                                        "volumeMounts": [
                                            {
                                                "name": users_secret_name,
                                                "mountPath": "/etc/clickhouse-server/users.d/",
                                                "readOnly": True,
                                            }
                                        ],
                                        "env": [
                                            {
                                                "name": "AWS_ROLE_ARN",
                                                "value": kwargs["irsa_role_arn"],
                                            },
                                            {
                                                "name": "AWS_WEB_IDENTITY_TOKEN_FILE",
                                                "value": "/var/run/secrets/eks.amazonaws.com/serviceaccount/token",
                                            },
                                        ],
                                    }
                                ],
                                "volumes": [
                                    {
                                        "name": users_secret_name,
                                        "secret": {
                                            "secretName": users_secret_name,
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                    "volumeClaimTemplates": [
                        {
                            "name": "clickhouse-data",
                            "spec": {
                                "accessModes": ["ReadWriteOnce"],
                                "storageClassName": storage_class,
                                "resources": {
                                    "requests": {
                                        "storage": hot_storage_size,
                                    }
                                },
                            },
                        }
                    ],
                },
            },
            opts=ResourceOptions(
                depends_on=[
                    keeper_statefulset,
                    users_secret,
                    vault_k8s_resources,
                ],
            ),
        )
    )


# Per-tool passwords from stack config (must be set with `pulumi config set --secret`)
admin_password = _require_password(
    clickhouse_config.get_secret("admin_password")
    or Output.secret("changeme"),  # pragma: allowlist secret
    "admin",
)
tensorzero_password = _require_password(
    clickhouse_config.get_secret("tensorzero_password")
    or Output.secret("changeme"),  # pragma: allowlist secret
    "tensorzero",
)
openlit_password = _require_password(
    clickhouse_config.get_secret("openlit_password")
    or Output.secret("changeme"),  # pragma: allowlist secret
    "openlit",
)
opik_password = _require_password(
    clickhouse_config.get_secret("opik_password")
    or Output.secret("changeme"),  # pragma: allowlist secret
    "opik",
)

############################################################
# S3 Cold Storage Bucket
############################################################
cold_bucket_name = f"ol-data-clickhouse-cold-{stack_info.env_suffix}"

cold_bucket_config = S3BucketConfig(
    bucket_name=cold_bucket_name,
    tags=aws_config.tags,
    versioning_enabled=False,
    server_side_encryption_enabled=True,
    sse_algorithm="AES256",
    intelligent_tiering_enabled=True,
    intelligent_tiering_days=1,  # Move to Intelligent-Tiering immediately
)

cold_bucket = OLBucket(
    f"clickhouse-cold-storage-{stack_info.env_suffix}",
    cold_bucket_config,
)

export("cold_bucket_name", cold_bucket.bucket_v2.bucket)
export("cold_bucket_arn", cold_bucket.bucket_v2.arn)

############################################################
# IRSA + Vault Auth Binding for ClickHouse
############################################################

# Build the IAM policy JSON as an Output to resolve bucket ARN values
clickhouse_s3_policy_json: Output[str] = Output.all(
    bucket_arn=cold_bucket.bucket_v2.arn,
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
                },
            ],
        }
    )
)

clickhouse_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="clickhouse",
        namespace=CLICKHOUSE_NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("clickhouse_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="clickhouse",
        vault_sync_service_account_names="clickhouse-vault",
        k8s_labels=k8s_labels,
    )
)

clickhouse_s3_iam_policy = aws.iam.Policy(
    f"clickhouse-s3-policy-{stack_info.env_suffix}",
    name=f"clickhouse-s3-policy-{stack_info.env_suffix}",
    path=f"/ol-data/clickhouse-s3-policy-{stack_info.env_suffix}/",
    policy=clickhouse_s3_policy_json,
    description="Policy for granting ClickHouse access to cold storage S3 bucket",
)

aws.iam.RolePolicyAttachment(
    f"clickhouse-s3-policy-attach-{stack_info.env_suffix}",
    policy_arn=clickhouse_s3_iam_policy.arn,
    role=clickhouse_app.irsa_role.name,
)

############################################################
# Vault KV Secrets — store credentials in Vault
############################################################
vault.kv.SecretV2(
    f"clickhouse-vault-kv-secrets-{stack_info.env_suffix}",
    mount=clickhouse_vault_kv_path,
    name="credentials",
    data_json=Output.all(
        admin=admin_password,
        tensorzero=tensorzero_password,
        openlit=openlit_password,
        opik=opik_password,
    ).apply(json.dumps),
)

############################################################
# VSO K8s Secret — users.xml with SHA256 password hashes
#
# ClickHouse reads user config from /etc/clickhouse-server/users.d/
# The VSO template computes SHA256 hashes of plaintext passwords using
# Sprig's sha256sum function so that plaintext never leaves Vault.
############################################################
USERS_XML_TEMPLATE = dedent("""\
    <clickhouse>
      <users>
        <admin>
          <password_sha256_hex>{{ get .Secrets "admin" | sha256sum }}</password_sha256_hex>
          <access_management>1</access_management>
          <profile>default</profile>
          <quota>default</quota>
        </admin>
        <tensorzero>
          <password_sha256_hex>{{ get .Secrets "tensorzero" | sha256sum }}</password_sha256_hex>
          <profile>llmops_profile</profile>
          <quota>llmops_quota</quota>
          <allow_databases>
            <database>tensorzero_db</database>
          </allow_databases>
        </tensorzero>
        <openlit>
          <password_sha256_hex>{{ get .Secrets "openlit" | sha256sum }}</password_sha256_hex>
          <profile>llmops_profile</profile>
          <quota>llmops_quota</quota>
          <allow_databases>
            <database>openlit_db</database>
          </allow_databases>
        </openlit>
        <opik>
          <password_sha256_hex>{{ get .Secrets "opik" | sha256sum }}</password_sha256_hex>
          <profile>llmops_profile</profile>
          <quota>llmops_quota</quota>
          <allow_databases>
            <database>opik_db</database>
          </allow_databases>
        </opik>
      </users>
    </clickhouse>
""")

users_secret_name = "clickhouse-users"  # pragma: allowlist secret  # noqa: S105
users_secret = OLVaultK8SSecret(
    f"clickhouse-users-secret-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="clickhouse-users-config",
        namespace=CLICKHOUSE_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=users_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=clickhouse_vault_kv_path,
        mount_type="kv-v2",
        path="credentials",
        templates={"users.xml": USERS_XML_TEMPLATE},
        refresh_after="1h",
        vaultauth=clickhouse_app.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=clickhouse_app.vault_k8s_resources,
    ),
)

############################################################
# ClickHouse Keeper — StatefulSet (3 or 1 replicas)
#
# Keeper provides distributed coordination (ZooKeeper-compatible protocol)
# required for ClickHouse replication. Uses EBS gp3 for its own storage since
# it stores only coordination logs (not bulk data) — NVMe not needed here.
############################################################
KEEPER_CLUSTER_SERVICE_NAME = "clickhouse-keeper"
keeper_config_xml = dedent("""\
    <clickhouse>
      <keeper_server>
        <tcp_port>2181</tcp_port>
        <server_id from_env="MY_ID" />
        <log_storage_path>/var/lib/clickhouse-keeper/coordination/log</log_storage_path>
        <snapshot_storage_path>/var/lib/clickhouse-keeper/coordination/snapshots</snapshot_storage_path>
        <coordination_settings>
          <raft_logs_level>warning</raft_logs_level>
          <operation_timeout_ms>10000</operation_timeout_ms>
          <session_timeout_ms>30000</session_timeout_ms>
        </coordination_settings>
        <raft_configuration>
          {raft_servers}
        </raft_configuration>
      </keeper_server>
    </clickhouse>
""")


def _build_raft_servers(n: int) -> str:
    """Generate Raft server entries for n Keeper pods."""
    servers = [
        f"<server>"
        f"<id>{i + 1}</id>"
        f"<hostname>clickhouse-keeper-{i}.clickhouse-keeper-headless.{CLICKHOUSE_NAMESPACE}.svc.cluster.local</hostname>"
        f"<port>9444</port>"
        f"</server>"
        for i in range(n)
    ]
    return "\n          ".join(servers)


keeper_statefulset = _create_keeper_resources(
    env_suffix=stack_info.env_suffix,
    namespace=CLICKHOUSE_NAMESPACE,
    labels=k8s_global_labels,
    replicas=keeper_replicas,
    image=keeper_image,
    use_io_optimized=use_io_optimized_nodes,
    ebs_storageclass_output=cluster_stack.get_output("ebs_storageclass"),
    fallback_storage_class=storage_class,
)

############################################################
# ClickHouseInstallation CRD
#
# The Altinity operator watches for ClickHouseInstallation resources and
# creates StatefulSets, Services, and ConfigMaps accordingly.
#
# Storage policy:
#   hot_local  (NVMe or EBS PVC at /var/lib/clickhouse/) — fast writes
#   cold_s3    (S3 bucket, IRSA credentials via pod service account)
#
# Users/databases are NOT defined here; they come from the users.xml K8s secret
# volume-mounted at /etc/clickhouse-server/users.d/.
############################################################
clickhouse_installation = _create_clickhouse_installation(
    env_suffix=stack_info.env_suffix,
    namespace=CLICKHOUSE_NAMESPACE,
    labels=k8s_global_labels,
    ch_replicas=ch_replicas,
    ch_image=ch_image,
    use_io_optimized=use_io_optimized_nodes,
    storage_class=storage_class,
    hot_storage_size=hot_storage_size,
    cold_bucket_name=cold_bucket.bucket_v2.bucket,
    users_secret_name=users_secret_name,
    irsa_role_arn=clickhouse_app.irsa_role.arn,
    keeper_statefulset=keeper_statefulset,
    users_secret=users_secret,
    vault_k8s_resources=clickhouse_app.vault_k8s_resources,
)

############################################################
# Create per-tool databases
# NOTE: ClickHouse SQL resources cannot be created declaratively via Pulumi
# since there is no ClickHouse Pulumi provider. The databases listed here must
# be created manually (or via an init job) after the cluster is up:
#
#   kubectl exec -it chi-clickhouse-default-0-0 -n clickhouse -- \
#     clickhouse-client --user admin --password <admin_password> \
#     --query "CREATE DATABASE IF NOT EXISTS tensorzero_db"
#
# Required databases: tensorzero_db, openlit_db, opik_db
############################################################

############################################################
# Networking — ClusterIP Service + NetworkPolicy
#
# The Altinity operator creates per-replica services automatically.
# We also create:
#   • A stable ClusterIP service (``clickhouse``) that load-balances across
#     all healthy ClickHouse replicas via the HTTP (8123) and Native (9000)
#     ports for intra-cluster LLMOps clients.
#   • A NetworkPolicy that restricts external access: only pods within the
#     ``clickhouse`` namespace and the LLMOps namespaces (tensorzero, openlit,
#     opik) may reach ClickHouse on the data ports; inter-replica and Keeper
#     traffic is allowed namespace-wide.
############################################################
clickhouse_client_service = kubernetes.core.v1.Service(
    f"clickhouse-client-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse",
        namespace=CLICKHOUSE_NAMESPACE,
        labels={**k8s_global_labels, "app": "clickhouse"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"clickhouse.altinity.com/chi": "clickhouse"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="http",
                port=8123,
                target_port=8123,
            ),
            kubernetes.core.v1.ServicePortArgs(
                name="native",
                port=9000,
                target_port=9000,
            ),
        ],
    ),
)

# NOTE: These namespaces must exist on the data EKS cluster and be included in
# the `eks:namespaces` list in the `infrastructure.aws.eks.data.*` stack
# configurations. If they are missing, this NetworkPolicy will not allow
# traffic from LLMOps workloads in those namespaces as intended.
LLMOPS_NAMESPACES = ["tensorzero", "openlit", "opik"]

clickhouse_network_policy = kubernetes.networking.v1.NetworkPolicy(
    f"clickhouse-network-policy-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse-allow-llmops",
        namespace=CLICKHOUSE_NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.networking.v1.NetworkPolicySpecArgs(
        pod_selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"clickhouse.altinity.com/chi": "clickhouse"},
        ),
        policy_types=["Ingress"],
        ingress=[
            # Allow same-namespace traffic (Keeper, operator, admin)
            kubernetes.networking.v1.NetworkPolicyIngressRuleArgs(
                from_=[
                    kubernetes.networking.v1.NetworkPolicyPeerArgs(
                        namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                            match_labels={
                                "kubernetes.io/metadata.name": CLICKHOUSE_NAMESPACE
                            },
                        )
                    )
                ],
            ),
            # Allow HTTP + Native from LLMOps namespaces
            kubernetes.networking.v1.NetworkPolicyIngressRuleArgs(
                from_=[
                    kubernetes.networking.v1.NetworkPolicyPeerArgs(
                        namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                            match_expressions=[
                                kubernetes.meta.v1.LabelSelectorRequirementArgs(
                                    key="kubernetes.io/metadata.name",
                                    operator="In",
                                    values=LLMOPS_NAMESPACES,
                                )
                            ]
                        )
                    )
                ],
                ports=[
                    kubernetes.networking.v1.NetworkPolicyPortArgs(port=8123),
                    kubernetes.networking.v1.NetworkPolicyPortArgs(port=9000),
                ],
            ),
            # Allow Prometheus scraping from monitoring namespace
            kubernetes.networking.v1.NetworkPolicyIngressRuleArgs(
                from_=[
                    kubernetes.networking.v1.NetworkPolicyPeerArgs(
                        namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                            match_labels={"kubernetes.io/metadata.name": "monitoring"},
                        )
                    )
                ],
                ports=[
                    kubernetes.networking.v1.NetworkPolicyPortArgs(port=8123),
                ],
            ),
        ],
    ),
)

############################################################
# Monitoring — ServiceMonitor for Prometheus Operator
#
# ClickHouse exposes Prometheus metrics at /metrics on the HTTP port (8123).
# The ServiceMonitor targets the stable ``clickhouse`` ClusterIP Service
# created above, so Prometheus scrapes all replicas through it.
# Requires the Prometheus Operator (monitoring.coreos.com/v1 CRDs) to be
# installed in the cluster (already present per EKS infrastructure stack).
############################################################
clickhouse_service_monitor = kubernetes.apiextensions.CustomResource(
    f"clickhouse-service-monitor-{stack_info.env_suffix}",
    api_version="monitoring.coreos.com/v1",
    kind="ServiceMonitor",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="clickhouse",
        namespace=CLICKHOUSE_NAMESPACE,
        labels={
            **k8s_global_labels,
            # Label required for Prometheus Operator to discover this ServiceMonitor
            "release": "prometheus",
        },
    ),
    spec={
        "selector": {
            "matchLabels": {"app": "clickhouse"},
        },
        "namespaceSelector": {"matchNames": [CLICKHOUSE_NAMESPACE]},
        "endpoints": [
            {
                "port": "http",
                "path": "/metrics",
                "scheme": "http",
                "interval": "30s",
                "scrapeTimeout": "10s",
                "relabelings": [
                    {
                        "sourceLabels": ["__meta_kubernetes_pod_name"],
                        "targetLabel": "pod",
                    },
                    {
                        "sourceLabels": ["__meta_kubernetes_namespace"],
                        "targetLabel": "namespace",
                    },
                ],
            }
        ],
    },
    opts=ResourceOptions(depends_on=[clickhouse_client_service]),
)

export("clickhouse_namespace", CLICKHOUSE_NAMESPACE)
export(
    "clickhouse_service",
    f"clickhouse.{CLICKHOUSE_NAMESPACE}.svc.cluster.local",
)
export("clickhouse_native_port", 9000)
export("clickhouse_http_port", 8123)
export(
    "keeper_service",
    f"clickhouse-keeper.{CLICKHOUSE_NAMESPACE}.svc.cluster.local",
)
export("cold_storage_bucket", cold_bucket_name)
export(
    "connection_guide",
    dedent(f"""\
        ClickHouse HTTP: http://clickhouse.{CLICKHOUSE_NAMESPACE}.svc.cluster.local:8123
        ClickHouse Native: clickhouse.{CLICKHOUSE_NAMESPACE}.svc.cluster.local:9000
        Keeper: clickhouse-keeper.{CLICKHOUSE_NAMESPACE}.svc.cluster.local:2181

        Tenants: tensorzero_db, openlit_db, opik_db
        Hot/cold cutoff: {hot_data_days} days (table TTL MOVE expressions)
        Cold storage: s3://{cold_bucket_name}/data/
    """),
)
