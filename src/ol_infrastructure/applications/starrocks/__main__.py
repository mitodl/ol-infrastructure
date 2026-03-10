from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference

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
from ol_infrastructure.lib.vault import get_vault_provider, setup_vault_provider

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

# Read OIDC credentials from Vault at deploy time for OAuth2 Security Integration setup
sso_secret = None
if starrocks_config.get_bool("oidc_enabled"):
    vault_provider = get_vault_provider(
        Config("vault").require("address"),
        Config("vault_server").require("env_namespace"),
        skip_child_token=None,
    )
    sso_secret = vault.generic.get_secret_output(
        path="secret-operations/sso/starrocks",
        opts=InvokeOptions(provider=vault_provider),
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

# Configure OAuth2 authentication via SQL Security Integration.
# The fe.conf approach is legacy; the current approach uses:
#   CREATE SECURITY INTEGRATION + ADMIN SET FRONTEND CONFIG authentication_chain
# Ref: https://docs.starrocks.io/docs/administration/user_privs/authentication/security_integration/
if sso_secret is not None:
    domain = starrocks_config.require("domain")
    fe_service = f"{stack_info.env_prefix}-starrocks-fe-service"

    # Build the SQL at deploy time with Vault values inlined.
    # Stored as a Secret (not ConfigMap) because it contains the OAuth2 client_secret.
    oauth2_sql_secret = kubernetes.core.v1.Secret(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-oauth2-sql-secret",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"starrocks-{stack_info.env_prefix}-oauth2-sql",
            namespace=namespace,
            labels=k8s_app_labels.model_dump(),
        ),
        string_data=sso_secret.data.apply(
            lambda data: {
                "setup.sql": (
                    "DROP SECURITY INTEGRATION IF EXISTS keycloak;\n"
                    "CREATE SECURITY INTEGRATION keycloak PROPERTIES (\n"
                    '  "type" = "authentication_oauth2",\n'
                    f'  "auth_server_url" = "{data["url"]}'
                    '/protocol/openid-connect/auth",\n'
                    f'  "token_server_url" = "{data["url"]}'
                    '/protocol/openid-connect/token",\n'
                    f'  "client_id" = "{data["client_id"]}",\n'
                    f'  "client_secret" = "{data["client_secret"]}",\n'
                    f'  "redirect_url" = "https://{domain}/api/oauth2",\n'
                    f'  "jwks_url" = "{data["url"]}/protocol/openid-connect/certs",\n'
                    '  "principal_field" = "preferred_username"\n'
                    ");\n"
                    'ADMIN SET FRONTEND CONFIG ("authentication_chain" = "keycloak");\n'
                )
            }
        ),
    )

    kubernetes.batch.v1.Job(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-oauth2-setup-job",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"starrocks-{stack_info.env_prefix}-oauth2-setup",
            namespace=namespace,
            labels=k8s_app_labels.model_dump(),
        ),
        spec=kubernetes.batch.v1.JobSpecArgs(
            ttl_seconds_after_finished=600,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=k8s_app_labels.model_dump(),
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="wait-for-fe",
                            image="busybox:1.36",
                            command=[
                                "sh",
                                "-c",
                                f"until nc -z {fe_service} 9030; "
                                "do echo 'Waiting for StarRocks FE...'; "
                                "sleep 5; done",
                            ],
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="configure-oauth2",
                            image="mysql:8.0",
                            command=[
                                "sh",
                                "-c",
                                f"mysql --default-auth=mysql_native_password"
                                f" -h {fe_service} -P 9030 -u root < /sql/setup.sql",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="MYSQL_PWD",
                                    value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                        secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                            name=starrocks_root_password_secret_name,
                                            key="password",
                                        )
                                    ),
                                ),
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="sql",
                                    mount_path="/sql",
                                    read_only=True,
                                ),
                            ],
                        ),
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="sql",
                            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                                secret_name=f"starrocks-{stack_info.env_prefix}-oauth2-sql",
                            ),
                        ),
                    ],
                    restart_policy="OnFailure",
                ),
            ),
        ),
        opts=ResourceOptions(
            depends_on=[starrocks_release, oauth2_sql_secret],
            delete_before_replace=True,
        ),
    )
