"""Deploy the StarRocks application to the data EKS cluster."""

from pathlib import Path
from typing import Any, cast

import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import iam

from bridge.lib.versions import STARROCKS_CHART_VERSION, STARROCKS_VERSION
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
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
from ol_infrastructure.lib.pulumi_helper import parse_stack, require_stack_output_value
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()
starrocks_config = Config("starrocks")

cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")
setup_k8s_provider(require_stack_output_value(cluster_stack, "kube_config"))
stateful_workload_storage = require_stack_output_value(
    cluster_stack, "stateful_workload_storage"
)
use_io_optimized_nodes = stateful_workload_storage["use_io_optimized_nodes"]
starrocks_data_storage_class = stateful_workload_storage["storage_class"]

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
        application_name="starrocks",
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
            "OIDC_JWKS_URI": '{{ get .Secrets "url" }}/protocol/openid-connect/certs',
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
# When enabled, the IRSA role is granted the data-lake query-engine policy so that
# StarRocks FE/CN pods (which use the annotated "starrocks" service account) can call
# the Glue Data Catalog API and read Iceberg data from the corresponding S3 buckets.
#
# After deploying, create the catalog in StarRocks by running the SQL exported as
# the "iceberg_catalog_sql" stack output (e.g. via the FE MySQL-compatible port 9030).
if starrocks_config.get_bool("enable_data_lake_integration"):
    aws_region = starrocks_config.get("aws_region") or "us-east-1"
    data_warehouse_stack = StackReference(
        f"infrastructure.aws.data_warehouse.{stack_info.name}"
    )
    iam.RolePolicyAttachment(
        f"starrocks-data-lake-policy-{stack_info.env_suffix}",
        policy_arn=data_warehouse_stack.require_output(
            "data_lake_query_engine_iam_policy_arn"
        ),
        role=starrocks_auth_binding.irsa_role.name,
        opts=ResourceOptions(parent=starrocks_auth_binding),
    )
    export(
        "iceberg_catalog_sql",
        # Uses the default AWS SDK credential chain (no instance profile, no explicit
        # role assumption). On EKS with IRSA, the pod already runs as the trust role
        # via AWS_ROLE_ARN + AWS_WEB_IDENTITY_TOKEN_FILE; specifying iam_role_arn
        # causes StarRocks to re-assume the same role and fail with a 403. Leaving
        # both use_instance_profile=false and iam_role_arn unset lets the SDK pick up
        # the web identity token credentials already in the environment.
        f"""CREATE EXTERNAL CATALOG ol_data_lake_iceberg
COMMENT 'MIT OL Data Lake Iceberg Catalog (AWS Glue / {stack_info.env_suffix})'
PROPERTIES(
    "type" = "iceberg",
    "iceberg.catalog.type" = "glue",
    "aws.glue.use_instance_profile" = "false",
    "aws.glue.region" = "{aws_region}",
    "aws.s3.use_instance_profile" = "false",
    "aws.s3.region" = "{aws_region}"
);""",
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
                "cpu": fe_config.get("cpu_request", "2000m"),
                "memory": fe_config.get("memory_request", "8Gi"),
            },
            "limits": {"memory": fe_config.get("memory_limit", "8Gi")},
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
                "cpu": be_config.get("cpu_request", "2000m"),
                "memory": be_config.get("memory_request", "8Gi"),
            },
            "limits": {"memory": be_config.get("memory_limit", "8Gi")},
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
                "cpu": cn_config.get("cpu_request", "2000m"),
                "memory": cn_config.get("memory_request", "8Gi"),
            },
            "limits": {"memory": cn_config.get("memory_limit", "8Gi")},
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

starrocks_tls_secret_name = f"{stack_info.env_prefix}-starrocks-tls-secret"
cert_manager_certificate = OLCertManagerCert(
    f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-tls-cert",
    cert_config=OLCertManagerCertConfig(
        application_name=f"{stack_info.env_prefix}-starrocks",
        k8s_namespace=namespace,
        k8s_labels=k8s_app_labels.model_dump(),
        create_apisixtls_resource=True,
        dest_secret_name=starrocks_tls_secret_name,
        dns_names=[starrocks_config.require("domain")],
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
