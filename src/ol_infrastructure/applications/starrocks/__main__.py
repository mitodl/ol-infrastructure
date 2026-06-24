"""Deploy the StarRocks application to the data EKS cluster."""

import json
from pathlib import Path
from typing import Any, cast

import pulumi_kubernetes as kubernetes
import pulumi_vault
from pulumi import Config, InvokeOptions, Output, ResourceOptions, export
from pulumi_aws import iam

from bridge.lib.versions import STARROCKS_CHART_VERSION, STARROCKS_VERSION
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

_vault_provider = setup_vault_provider()
stack_info = parse_stack()
starrocks_config = Config("starrocks")

cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
setup_k8s_provider(require_stack_output_value(cluster_stack, "kube_config"))
stateful_workload_storage = require_stack_output_value(
    cluster_stack, "stateful_workload_storage"
)
use_io_optimized_nodes = stateful_workload_storage["use_io_optimized_nodes"]
starrocks_data_storage_class = stateful_workload_storage["storage_class"]

kms_stack = make_stack_reference(projects.KMS, stack_info.name)
s3_kms_key = kms_stack.require_output("kms_s3_data_analytics_key")

starrocks_env = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(tags={"OU": "data", "Environment": starrocks_env})

namespace = "starrocks"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(namespace, ns)
)

k8s_app_labels = K8sAppLabels(
    application=Application.starrocks,
    product=Product.data,
    service=Services.starrocks,
    ou=BusinessUnit.data,
    source_repository="https://github.com/StarRocks/starrocks-kubernetes-operator",
    stack=stack_info,
)

io_optimized_node_selector = {"ol.mit.edu/io_optimized": "true"}
io_optimized_tolerations = [
    {
        "key": "ol.mit.edu/io-workload",
        "operator": "Equal",
        "value": "true",
        "effect": "NoSchedule",
    }
]
io_optimized_node_affinity = {
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

starrocks_root_password_secret_name = f"{stack_info.env_prefix}-starrocks-root-password"
starrocks_root_password_secret = kubernetes.core.v1.Secret(
    f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-root-password-secret",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=starrocks_root_password_secret_name,
        namespace=namespace,
        labels=k8s_app_labels.model_dump(),
    ),
    string_data={"password": starrocks_config.require("root_password")},
)

# Configure Vault integration for OIDC credentials using OLEKSAuthBinding
starrocks_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name=f"starrocks-{stack_info.env_prefix}",
        namespace=namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        vault_policy_path=Path(__file__).parent.joinpath("starrocks_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="starrocks",
        vault_sync_service_account_names=["starrocks-vault"],
        k8s_labels=k8s_app_labels,
        # Pre-create the SA so the IRSA role-ARN annotation is present before the
        # StarRocks operator starts and assigns this SA to FE/CN/BE pods.
        create_irsa_service_account=True,
    )
)

# Create OIDC configuration secret if enabled
if starrocks_config.get_bool("oidc_enabled"):
    oidc_config_secret_name = f"{stack_info.env_prefix}-starrocks-oidc-config"
    oidc_config_secret_config = OLVaultK8SStaticSecretConfig(
        name="starrocks-oidc-config",
        namespace=namespace,
        dest_secret_labels=k8s_app_labels.model_dump(),
        dest_secret_name=oidc_config_secret_name,
        labels=k8s_app_labels.model_dump(),
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/starrocks",
        restart_target_kind="StatefulSet",
        restart_target_name=f"{stack_info.env_prefix}-starrocks-fe",
        templates={
            "OIDC_ISSUER_URL": '{{ get .Secrets "url" }}',
            "OIDC_CLIENT_ID": '{{ get .Secrets "client_id" }}',
            "OIDC_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            "OIDC_JWKS_URL": '{{ get .Secrets "url" }}/protocol/openid-connect/certs',
        },
        vaultauth=starrocks_auth_binding.vault_k8s_resources.auth_name,
    )
    oidc_config_secret = OLVaultK8SSecret(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-oidc-config-secret",
        oidc_config_secret_config,
        opts=ResourceOptions(
            delete_before_replace=True,
            parent=starrocks_auth_binding.vault_k8s_resources,
        ),
    )

# Iceberg catalog integration via AWS Glue
# When enabled, the IRSA role is granted the data-lake query-engine policy for each
# environment so that StarRocks FE/CN pods (which use the annotated "starrocks" service
# account) can call the Glue Data Catalog API and read Iceberg data from the
# corresponding S3 buckets.
#
# Both QA and production catalogs are registered in every StarRocks instance
# (see substructure/starrocks _DATA_LAKE_ENVS), so the IRSA role needs query-engine
# access to both environments' Glue catalogs and S3 buckets.
#
# The Iceberg external catalogs are created and maintained by the substructure stack
# (substructure/starrocks) using the pulumi-command local.Command resource.
_DATA_LAKE_ENVS = ("QA", "Production")

if starrocks_config.get_bool("enable_data_lake_integration"):
    for _data_lake_env in _DATA_LAKE_ENVS:
        _dw_stack = make_stack_reference(projects.DATA_WAREHOUSE, _data_lake_env)
        iam.RolePolicyAttachment(
            f"starrocks-data-lake-policy-{stack_info.env_suffix}-{_data_lake_env}",
            policy_arn=_dw_stack.require_output(
                "data_lake_query_engine_iam_policy_arn"
            ),
            role=starrocks_auth_binding.irsa_role.name,
            opts=ResourceOptions(parent=starrocks_auth_binding),
        )

# AWS Java SDK v1 (bundled with StarRocks 4.x) does not automatically resolve
# AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE env vars through the default credential
# chain when running inside the JVM. Passing them explicitly as JVM system properties
# via JAVA_TOOL_OPTIONS ensures WebIdentityTokenFileCredentialsProvider can resolve
# IRSA credentials for Glue metadata and S3 data-access calls.
irsa_jvm_opts: Output[str] | None = None
if starrocks_config.get_bool("enable_data_lake_integration"):
    irsa_jvm_opts = starrocks_auth_binding.irsa_role.arn.apply(
        lambda arn: (
            f"-Daws.roleArn={arn}"
            " -Daws.webIdentityTokenFile="
            "/var/run/secrets/eks.amazonaws.com/serviceaccount/token"
            " -Daws.roleSessionName=starrocks-glue"
        )
    )

# Shared-data (CN) mode requires an S3 bucket for persistent cluster state.
# The bucket name follows the pattern ol-starrocks-<env_prefix>-<env_suffix>,
# e.g. ol-starrocks-lakehouse-qa.
#
# The IRSA role already covers Glue + data-lake S3 (read); here we add an
# inline policy granting full read/write on the dedicated shared-data bucket so
# FE and CN pods can write tablet data, metadata snapshots, and compaction
# output without relying on the broader data-lake policy.
aws_region = starrocks_config.get("aws_region") or "us-east-1"
shared_data_bucket: OLBucket | None = None
shared_data_bucket_name: str | None = None
if starrocks_config.get_bool("use_cn"):
    shared_data_bucket_name = (
        f"ol-starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}"
    )
    shared_data_bucket = OLBucket(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-shared-data-bucket",
        S3BucketConfig(
            tags=aws_config.tags,
            bucket_name=shared_data_bucket_name,
            server_side_encryption_enabled=True,
            kms_key_id=s3_kms_key["id"],
            bucket_key_enabled=True,
        ),
        opts=ResourceOptions(parent=starrocks_auth_binding),
    )
    iam.RolePolicy(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-shared-data-s3-policy",
        role=starrocks_auth_binding.irsa_role.name,
        policy=s3_kms_key["arn"].apply(
            lambda kms_arn: json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:GetObject",
                                "s3:PutObject",
                                "s3:DeleteObject",
                            ],
                            "Resource": f"arn:aws:s3:::{shared_data_bucket_name}/*",
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "s3:ListBucket",
                                "s3:GetBucketLocation",
                            ],
                            "Resource": f"arn:aws:s3:::{shared_data_bucket_name}",
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "kms:GenerateDataKey",
                                "kms:Decrypt",
                            ],
                            "Resource": kms_arn,
                        },
                    ],
                }
            )
        ),
        opts=ResourceOptions(parent=starrocks_auth_binding),
    )

if starrocks_config.get_bool("use_be") and starrocks_config.get_bool("use_cn"):
    msg = (
        "StarRocks can be deployed in either shared-nothing (BE) or shared-storage (CN)"
        " mode, but not both simultaneously."
    )
    raise ValueError(msg)

if not starrocks_config.get_bool("use_be") and not starrocks_config.get_bool("use_cn"):
    msg = (
        "StarRocks can be deployed in either shared-nothing (BE) or shared-storage (CN)"
        " mode. At least one must be specified."
    )
    raise ValueError(msg)

# SSL for client connections to the FE MySQL port (9030).
#
# Requires StarRocks >= 3.4.1. When enabled:
#   - cert-manager adds a PKCS12 keystore to the TLS secret.
#   - FE pods mount the secret and configure fe.conf with the keystore path/password.
#   - Set ssl_force_secure_transport=true only after updating all clients (e.g. Vault
#     JDBC URL) to use useSSL=true, as it rejects plain-text connections.
# Note: once PR #732 in starrocks-kubernetes-operator merges, CN pods will additionally
#   need cnEnvVars FE_TLS_MODE=skip-verify (or preferred) for the operator's internal
#   CN→FE MySQL connection to use TLS.
ssl_enabled = starrocks_config.get_bool("ssl_enabled") or False
ssl_force_secure_transport = (
    starrocks_config.get_bool("ssl_force_secure_transport") or False
)
# Defined here (rather than alongside OLCertManagerCert) so the SSL FE spec block and
# the cert-manager call below can both reference the same secret name.
starrocks_tls_secret_name = f"{stack_info.env_prefix}-starrocks-tls-secret"

ssl_keystore_password_secret: kubernetes.core.v1.Secret | None = None
ssl_keystore_password: Output[str] | None = None
ssl_keystore_password_secret_name: str | None = None
if ssl_enabled:
    ssl_keystore_password = starrocks_config.require_secret("ssl_keystore_password")
    ssl_keystore_password_secret_name = (
        f"{stack_info.env_prefix}-starrocks-ssl-keystore-password"
    )
    ssl_keystore_password_secret = kubernetes.core.v1.Secret(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-ssl-keystore-password-secret",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=ssl_keystore_password_secret_name,
            namespace=namespace,
            labels=k8s_app_labels.model_dump(),
        ),
        string_data={"password": ssl_keystore_password},
    )

# Ref: https://github.com/StarRocks/starrocks-kubernetes-operator/blob/main/helm-charts/charts/kube-starrocks/charts/starrocks/values.yaml
fe_config = starrocks_config.get_object("fe_config") or {}
starrocks_values: dict[str, Any] = {
    "nameOverride": f"{stack_info.env_prefix}-starrocks",
    "initPassword": {
        "enabled": True,
        "passwordSecret": starrocks_root_password_secret_name,
    },
    "timeZone": "UTC",
    "metrics": {
        "serviceMonitor": {
            "enabled": False,
        },
    },
    "starrocksCluster": {
        "enabledBe": starrocks_config.get_bool("use_be"),
        "enabledCn": starrocks_config.get_bool("use_cn"),
    },
    "starrocksFESpec": {
        "replicas": fe_config.get("replicas", 3),
        "image": {"tag": STARROCKS_VERSION},
        "serviceAccount": "starrocks",
        "runAsNonRoot": True,
        "service": {
            "type": "ClusterIP",
        },
        "resources": {
            "requests": {
                # StarRocks recommends 8 CPU cores and 16 GB RAM per FE node.
                # Ref: https://docs.starrocks.io/docs/deployment/plan_cluster/
                "cpu": fe_config.get("cpu_request", "8000m"),
                "memory": fe_config.get("memory_request", "16Gi"),
            },
            "limits": {
                "cpu": fe_config.get(
                    "cpu_limit", fe_config.get("cpu_request", "8000m")
                ),
                "memory": fe_config.get("memory_limit", "16Gi"),
            },
        },
        "storageSpec": {
            "name": f"{stack_info.env_prefix}-fe-storage",
            "storageClassName": "ebs-gp3-sc",
            "storageSize": fe_config.get("storage", "100Gi"),
            "logStorageSize": fe_config.get("log_storage", "100Gi"),
        },
        **(
            {"feEnvVars": [{"name": "JAVA_TOOL_OPTIONS", "value": irsa_jvm_opts}]}
            if irsa_jvm_opts is not None
            else {}
        ),
    },
}

# Placeholder ConfigMap for the file-based group provider managed by the substructure
# stack (substructure/starrocks keycloak_group_sync.py).  Created here so the FE pods
# have the volume available at startup; the substructure stack populates groups.txt
# via kubectl apply after each pulumi up.  ignore_changes=["data"] prevents this stack
# from overwriting the substructure-managed content on subsequent runs.
if starrocks_config.get_bool("oidc_enabled"):
    _oidc_group_cm_name = f"{stack_info.env_prefix}-starrocks-oidc-groups"
    kubernetes.core.v1.ConfigMap(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-oidc-groups-cm",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=_oidc_group_cm_name,
            namespace=namespace,
            labels=k8s_app_labels.model_dump(),
        ),
        data={"groups.txt": ""},
        opts=ResourceOptions(ignore_changes=["data"]),
    )
    _fe_oidc_spec = cast(dict[str, Any], starrocks_values["starrocksFESpec"])
    _fe_oidc_spec["configMaps"] = [
        {"name": _oidc_group_cm_name, "mountPath": "/opt/starrocks/fe/conf/groups"}
    ]

if starrocks_config.get_bool("use_be"):
    # Shared-nothing configuration
    be_config = starrocks_config.get_object("be_config") or {}
    starrocks_values["starrocksBeSpec"] = {
        "replicas": be_config.get("replicas", 3),
        "imagePullPolicy": "IfNotPresent",
        "image": {"tag": STARROCKS_VERSION},
        "serviceAccount": "starrocks",
        "runAsNonRoot": True,
        "resources": {
            "requests": {
                # StarRocks recommends 16 CPU cores and 64 GB RAM per BE node.
                # 32Gi is used as a practical minimum; increase for large datasets.
                # Ref: https://docs.starrocks.io/docs/deployment/plan_cluster/
                "cpu": be_config.get("cpu_request", "16000m"),
                "memory": be_config.get("memory_request", "32Gi"),
            },
            "limits": {
                "cpu": be_config.get(
                    "cpu_limit", be_config.get("cpu_request", "16000m")
                ),
                "memory": be_config.get("memory_limit", "32Gi"),
            },
        },
        "storageSpec": {
            "name": f"{stack_info.env_prefix}-be-storage",
            "storageClassName": starrocks_data_storage_class,
            "storageSize": be_config.get("storage", "1Ti"),
            "logStorageSize": be_config.get("log_storage", "100Gi"),
        },
        **(
            {"beEnvVars": [{"name": "JAVA_TOOL_OPTIONS", "value": irsa_jvm_opts}]}
            if irsa_jvm_opts is not None
            else {}
        ),
    }
    if use_io_optimized_nodes:
        starrocks_be_spec = cast(dict[str, Any], starrocks_values["starrocksBeSpec"])
        starrocks_be_spec["nodeSelector"] = io_optimized_node_selector
        starrocks_be_spec["tolerations"] = io_optimized_tolerations
        starrocks_be_spec["affinity"] = io_optimized_node_affinity

if starrocks_config.get_bool("use_cn"):
    # shared storage configuration
    cn_config = starrocks_config.get_object("cn_config") or {}
    starrocks_values["starrocksCnSpec"] = {
        "imagePullPolicy": "IfNotPresent",
        "image": {"tag": STARROCKS_VERSION},
        "serviceAccount": "starrocks",
        "runAsNonRoot": True,
        "resources": {
            "requests": {
                # StarRocks recommends 16 CPU cores and 64 GB RAM per CN node.
                # 32Gi is used as a practical minimum; increase for large datasets.
                # Ref: https://docs.starrocks.io/docs/deployment/plan_cluster/
                "cpu": cn_config.get("cpu_request", "16000m"),
                "memory": cn_config.get("memory_request", "32Gi"),
            },
            "limits": {
                "cpu": cn_config.get(
                    "cpu_limit", cn_config.get("cpu_request", "16000m")
                ),
                "memory": cn_config.get("memory_limit", "32Gi"),
            },
        },
        "autoScalingPolicy": {
            "minReplicas": cn_config.get("min_replicas", 1),
            "maxReplicas": cn_config.get("max_replicas", 10),
            "hpaPolicy": {
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": cn_config.get(
                                    "cpu_target_utilization_percentage", 80
                                ),
                            },
                        },
                    },
                ],
            },
        },
        **(
            {"cnEnvVars": [{"name": "JAVA_TOOL_OPTIONS", "value": irsa_jvm_opts}]}
            if irsa_jvm_opts is not None
            else {}
        ),
    }

# When SSL or CN shared-data mode is active we must supply a complete fe.conf
# because setting starrocksFESpec.config replaces the chart's default fe.conf
# entirely.  The base config below is sourced from the starrocks Helm chart
# defaults and must be reviewed whenever STARROCKS_CHART_VERSION is bumped.
# Ref: starrocks/values.yaml starrocksFESpec.config in the operator Helm chart.
#
# NOTE: The SSL keystore password appears in fe.conf (→ K8s ConfigMap). This is
# an inherent limitation of StarRocks' SSL design; the password protects the
# keystore file itself, not user credentials. Keep it scoped as a Pulumi secret.
if (
    ssl_enabled
    or starrocks_config.get_bool("use_cn")
    or starrocks_config.get_bool("use_be")
) and (STARROCKS_CHART_VERSION != "1.11.5"):
    msg = (
        f"_FE_CONFIG_BASE was sourced from chart 1.11.5; review defaults for"
        f" {STARROCKS_CHART_VERSION} before deploying with SSL or CN enabled"
    )
    raise ValueError(msg)
# Set JVM heap to 87.5 % of the container memory limit to leave headroom for
# off-heap allocations (metaspace, code cache, direct buffers, GC overhead).
# Configurable via starrocks:fe_config:jvm_heap_mb so operators can tune it
# when the container memory limit is changed in the stack YAML.
_fe_memory_limit_gi = int(str(fe_config.get("memory_limit", "16Gi")).rstrip("Gi"))
fe_jvm_heap_mb: int = fe_config.get(
    "jvm_heap_mb", int(_fe_memory_limit_gi * 1024 * 0.875)
)
# new_planner_optimize_timeout is the per-query optimizer time budget (ms).
# StarRocks default is 3000 ms, which is often too short when the FE must
# fetch Iceberg/Hive partition metadata from AWS Glue before it can finish
# planning.  30 s is a reasonable ceiling for data-lake workloads; lower it
# if you want fail-fast behaviour for purely local queries.
# Configurable via starrocks:fe_config:planner_optimize_timeout_ms.
fe_planner_optimize_timeout_ms: int = fe_config.get(
    "planner_optimize_timeout_ms", 30000
)
# background_refresh_metadata_enable instructs the FE to continuously sync
# all external catalog (Iceberg/Glue) metadata in the background so that
# tables added or modified in Glue are visible to StarRocks without a manual
# REFRESH CATALOG command.  10 minutes is short enough to pick up new
# dbt-materialized tables within one Dagster run cycle, long enough to avoid
# hammering the Glue API.  Configurable via
# starrocks:fe_config:background_refresh_metadata_interval_ms.
fe_background_refresh_interval_ms: int = fe_config.get(
    "background_refresh_metadata_interval_ms", 600_000
)
_FE_CONFIG_BASE = (
    "LOG_DIR = ${STARROCKS_HOME}/log\n"
    'DATE = "$(date +%Y%m%d-%H%M%S)"\n'
    f'JAVA_OPTS="-Dlog4j2.formatMsgNoLookups=true -Xmx{fe_jvm_heap_mb}m -XX:+UseG1GC'
    ' -Xlog:gc*:${LOG_DIR}/fe.gc.log.$DATE:time"\n'
    "http_port = 8030\n"
    "rpc_port = 9020\n"
    "query_port = 9030\n"
    "edit_log_port = 9010\n"
    "mysql_service_nio_enabled = true\n"
    "sys_log_level = INFO\n"
    "min_graceful_exit_time_second = 25\n"
    f"new_planner_optimize_timeout = {fe_planner_optimize_timeout_ms}\n"
    "background_refresh_metadata_enable = true\n"
    "background_refresh_metadata_interval_millis"
    f" = {fe_background_refresh_interval_ms}\n"
    # Keycloak exposes preferred_username (human-readable) rather than sub (UUID).
    # Set FE-level defaults so both the OAuth2 browser-redirect flow and the JWT
    # client-plugin flow map to the same username regardless of per-user settings.
    "oauth2_principal_field = preferred_username\n"
    "jwt_principal_field = preferred_username\n"
)


def _build_fe_config(  # noqa: PLR0913
    pwd: str | None,
    force_str: str,
    bucket_name: str | None,
    oidc_issuer_url: str | None = None,
    oidc_client_id: str | None = None,
    oidc_client_secret: str | None = None,
    oidc_redirect_url: str | None = None,
) -> str:
    """Assemble a complete fe.conf from optional SSL, shared-data, and OIDC sections.

    Both sections are optional and can be combined.  When neither is active the
    caller should not set starrocksFESpec.config at all (chart defaults apply).

    run_mode is always set explicitly:
      - shared_data  when use_cn=true  (CN nodes, S3-backed tablet storage)
      - shared_nothing when use_be=true (BE nodes, local disk storage)
    This prevents silent run_mode mismatches if existing FE meta was created
    under a different mode — StarRocks aborts on startup when run_mode in
    fe.conf disagrees with the mode recorded in its BDB metadata.

    SSL keystore password is validated here to prevent fe.conf corruption:
    StarRocks' config parser is line-oriented, so embedded newlines or
    leading/trailing whitespace would silently break the generated file and
    prevent FE startup.

    When oidc_issuer_url is provided, the full set of oauth2_* FE params is
    written so that the StarRocks web UI shows the "OAuth2 Login" button.
    The client secret ends up in a ConfigMap (StarRocks Helm chart limitation);
    access is RBAC-gated and marked as a Pulumi secret so it is not stored
    in plaintext Pulumi state.
    """
    conf = _FE_CONFIG_BASE

    if oidc_issuer_url is not None:
        _oidc_base = f"{oidc_issuer_url}/protocol/openid-connect"
        conf += (
            # oauth2_* — web UI "OAuth2 Login" button (browser-redirect flow).
            # StarRocks FE exchanges the authorization code server-side using
            # these credentials; the id_token is stored on the connection context
            # and forwarded to Iceberg REST catalogs when security = JWT.
            f"oauth2_auth_server_url = {_oidc_base}/auth\n"
            f"oauth2_token_server_url = {_oidc_base}/token\n"
            f"oauth2_client_id = {oidc_client_id}\n"
            f"oauth2_client_secret = {oidc_client_secret}\n"
            f"oauth2_redirect_url = {oidc_redirect_url}\n"
            f"oauth2_jwks_url = {_oidc_base}/certs\n"
            f"oauth2_required_issuer = {oidc_issuer_url}\n"
            f"oauth2_required_audience = {oidc_client_id}\n"
            # jwt_* — mysql CLI client-plugin flow (authentication_jwt).
            # The client pre-fetches an id_token (e.g. via starrocks-auth PKCE)
            # and sends it over the MySQL wire using the
            # authentication_openid_connect_client plugin (MySQL 9.2+).
            # No client_secret needed; the server verifies the JWT signature
            # against the JWKS and maps preferred_username to the SR identity.
            f"jwt_jwks_url = {_oidc_base}/certs\n"
            f"jwt_required_issuer = {oidc_issuer_url}\n"
        )

    if bucket_name is not None:
        # shared_data (CN) mode: FE manages all tablet storage in S3.
        # run_mode must be set before SSL so StarRocks reads it early.
        # aws_s3_use_instance_profile=true works for both EC2 instance profiles
        # and EKS IRSA — StarRocks uses the AWS SDK default credential chain.
        # enable_load_volume_from_conf defaults to false in StarRocks >= 3.4.1
        # and must be set explicitly so FE bootstraps the built-in storage volume
        # from these fe.conf settings on first startup.
        conf += (
            "run_mode = shared_data\n"
            "cloud_native_meta_port = 6090\n"
            "cloud_native_storage_type = S3\n"
            f"aws_s3_path = {bucket_name}\n"
            f"aws_s3_region = {aws_region}\n"
            f"aws_s3_endpoint = https://s3.{aws_region}.amazonaws.com\n"
            "aws_s3_use_instance_profile = true\n"
            "enable_load_volume_from_conf = true\n"
        )
    else:
        # shared_nothing (BE) mode: explicit to prevent ambiguity and guard
        # against accidental run_mode migration if use_cn is ever toggled.
        conf += "run_mode = shared_nothing\n"

    if pwd is not None:
        if "\n" in pwd or "\r" in pwd:
            msg = "starrocks:ssl_keystore_password must not contain newline characters"
            raise ValueError(msg)
        pwd = pwd.strip()
        if not pwd:
            msg = "starrocks:ssl_keystore_password must not be empty or whitespace-only"
            raise ValueError(msg)
        conf += (
            "ssl_keystore_location = /etc/starrocks/ssl/keystore.p12\n"
            f"ssl_keystore_password = {pwd}\n"
            f"ssl_key_password = {pwd}\n"
            f"ssl_force_secure_transport = {force_str}\n"
        )

    return conf


_needs_fe_config = (
    ssl_enabled
    or starrocks_config.get_bool("use_cn")
    or starrocks_config.get_bool("use_be")
)
if _needs_fe_config:
    _force_str = "TRUE" if ssl_force_secure_transport else "FALSE"
    fe_spec = cast(dict[str, Any], starrocks_values["starrocksFESpec"])
    _domain = starrocks_config.require("domain")

    # Pull OIDC client credentials from Vault when OIDC is enabled so the FE web
    # UI can display the "OAuth2 Login" button (requires oauth2_* in fe.conf).
    _oidc_vault_data: Output | None = None
    if starrocks_config.get_bool("oidc_enabled"):
        _oidc_vault_data = pulumi_vault.generic.get_secret_output(
            path="secret-operations/sso/starrocks",
            with_lease_start_time=False,
            opts=InvokeOptions(provider=_vault_provider),
        ).data

    if ssl_enabled and ssl_keystore_password is not None:
        fe_spec["secrets"] = [
            {"name": starrocks_tls_secret_name, "mountPath": "/etc/starrocks/ssl"}
        ]
        if _oidc_vault_data is not None:
            fe_spec["config"] = Output.all(
                pwd=ssl_keystore_password, oidc=_oidc_vault_data
            ).apply(
                lambda args: _build_fe_config(
                    args["pwd"],
                    _force_str,
                    shared_data_bucket_name,
                    oidc_issuer_url=args["oidc"]["url"],
                    oidc_client_id=args["oidc"]["client_id"],
                    oidc_client_secret=args["oidc"]["client_secret"],
                    oidc_redirect_url=f"https://{_domain}/api/oauth2",
                )
            )
        else:
            fe_spec["config"] = ssl_keystore_password.apply(
                lambda pwd: _build_fe_config(pwd, _force_str, shared_data_bucket_name)
            )
    elif _oidc_vault_data is not None:
        fe_spec["config"] = _oidc_vault_data.apply(
            lambda oidc: _build_fe_config(
                None,
                _force_str,
                shared_data_bucket_name,
                oidc_issuer_url=oidc["url"],
                oidc_client_id=oidc["client_id"],
                oidc_client_secret=oidc["client_secret"],
                oidc_redirect_url=f"https://{_domain}/api/oauth2",
            )
        )
    else:
        # CN-only path (no SSL, no OIDC): no Output dependencies, build synchronously.
        fe_spec["config"] = _build_fe_config(None, _force_str, shared_data_bucket_name)

starrocks_release = kubernetes.helm.v3.Release(
    f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name=f"{stack_info.env_prefix}-starrocks",
        chart="starrocks",
        version=STARROCKS_CHART_VERSION,
        namespace=namespace,
        cleanup_on_fail=True,
        skip_await=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://starrocks.github.io/starrocks-kubernetes-operator",
        ),
        values=starrocks_values,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[starrocks_root_password_secret, starrocks_auth_binding],
    ),
)

cert_manager_certificate = OLCertManagerCert(
    f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-tls-cert",
    cert_config=OLCertManagerCertConfig(
        application_name=f"{stack_info.env_prefix}-starrocks",
        k8s_namespace=namespace,
        k8s_labels=k8s_app_labels.model_dump(),
        create_apisixtls_resource=True,
        dest_secret_name=starrocks_tls_secret_name,
        dns_names=[starrocks_config.require("domain")],
        pkcs12_keystore_password_secret_name=ssl_keystore_password_secret_name,
    ),
    opts=ResourceOptions(
        depends_on=[ssl_keystore_password_secret]
        if ssl_keystore_password_secret
        else []
    ),
)

starrocks_apisix_httproute = OLApisixHTTPRoute(
    f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-apisix-httproute",
    route_configs=[
        OLApisixHTTPRouteConfig(
            route_name=f"{stack_info.env_prefix}-starrocks",
            hosts=[starrocks_config.require("domain")],
            paths=["/*"],
            backend_service_name=f"{stack_info.env_prefix}-starrocks-fe-service",
            backend_service_port=8030,
            plugins=[],
        ),
    ],
    k8s_namespace=namespace,
    k8s_labels=k8s_app_labels.model_dump(),
)

# Internal NLB exposing the StarRocks FE MySQL port (9030) to the data VPC so that
# Vault — running on EC2 in the operations VPC, which is peered with the data VPC —
# can reach StarRocks to manage dynamic database credentials.
FE_MYSQL_PORT = 9030
fe_mysql_nlb_service = kubernetes.core.v1.Service(
    f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-fe-mysql-nlb",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"{stack_info.env_prefix}-starrocks-fe-mysql",
        namespace=namespace,
        labels=k8s_app_labels.model_dump(),
        annotations={
            "service.beta.kubernetes.io/aws-load-balancer-type": "external",
            "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
            "service.beta.kubernetes.io/aws-load-balancer-scheme": "internal",
            "service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled": "true",  # noqa: E501
        },
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="LoadBalancer",
        selector={
            "app.kubernetes.io/component": "fe",
            "app.starrocks.ownerreference/name": (
                f"{stack_info.env_prefix}-starrocks-fe"
            ),
        },
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="mysql",
                port=FE_MYSQL_PORT,
                target_port=FE_MYSQL_PORT,
                protocol="TCP",
            ),
        ],
    ),
    opts=ResourceOptions(depends_on=[starrocks_release]),
)

export(
    "fe_mysql_host",
    fe_mysql_nlb_service.status.apply(
        lambda s: (
            s.load_balancer.ingress[0].hostname
            if s and s.load_balancer and s.load_balancer.ingress
            else ""
        )
    ),
)

# Export the root password as a secret so the substructure/starrocks stack can
# reference it via StackReference to prime the Vault database connection.
export("root_password_secret", starrocks_config.require_secret("root_password"))

if shared_data_bucket is not None:
    export(
        "shared_data_bucket_name",
        shared_data_bucket.bucket_v2.bucket,
    )
