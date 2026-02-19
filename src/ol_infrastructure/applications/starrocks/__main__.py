from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference

from bridge.lib.versions import STARROCKS_CHART_VERSION
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
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
stack_info = parse_stack()
starrocks_config = Config("starrocks")

cluster_stack = StackReference(f"infrastructure.aws.eks.data.{stack_info.name}")
setup_k8s_provider(cluster_stack.require_output("kube_config"))

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
starrocks_values = {
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
    },
}

if starrocks_config.get_bool("use_be"):
    # Shared-nothing configuration
    be_config = starrocks_config.get_object("be_config") or {}
    starrocks_values["starrocksBeSpec"] = {
        "replicas": be_config.get("replicas", 3),
        "imagePullPolicy": "IfNotPresent",
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
            "storageClassName": "ebs-gp3-sc",
            "storageSize": be_config.get("storage", "1Ti"),
            "logStorageSize": be_config.get("log_storage", "100Gi"),
        },
    }

if starrocks_config.get_bool("use_cn"):
    # shared storage configuration
    cn_config = starrocks_config.get_object("cn_config") or {}
    starrocks_values["starrocksCnSpec"] = {
        "imagePullPolicy": "IfNotPresent",
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
        delete_before_replace=True, depends_on=[starrocks_root_password_secret]
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
