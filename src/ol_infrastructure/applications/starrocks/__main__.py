from pathlib import Path
from typing import Any

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
    cached_image_uri,
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

# Vault/K8s auth binding: provisions the IAM role, Vault policy, and K8s auth
# backend role that allows the VSO service account (starrocks-vault) to sync
# secrets from Vault into the starrocks namespace.
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

# Sync SSO credentials from Vault into a K8s Secret via the Vault Secrets Operator.
# The VSO service account (starrocks-vault) is bound by starrocks_auth_binding above.
sso_k8s_secret_name = None
sso_k8s_secret = None
if starrocks_config.get_bool("oidc_enabled"):
    sso_k8s_secret_name = f"{stack_info.env_prefix}-starrocks-sso"
    sso_k8s_secret = OLVaultK8SSecret(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-sso-secret",
        resource_config=OLVaultK8SStaticSecretConfig(
            name="starrocks-sso-config",
            namespace=namespace,
            labels=k8s_app_labels.model_dump(),
            dest_secret_labels=k8s_app_labels.model_dump(),
            dest_secret_name=sso_k8s_secret_name,
            mount="secret-operations",
            mount_type="kv-v1",
            path="sso/starrocks",
            templates={
                "SSO_URL": '{{ get .Secrets "url" }}',
                "SSO_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                "SSO_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            },
            refresh_after="1h",
            vaultauth=starrocks_auth_binding.vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[starrocks_auth_binding],
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

if starrocks_config.get_bool("oidc_enabled"):
    # authentication_chain in fe.conf applies at every FE startup, ensuring the
    # Keycloak auth chain is active even if BDB metadata is reset.
    starrocks_values["starrocksFESpec"]["config"] = "authentication_chain = keycloak\n"

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
# Uses idempotent ALTER-if-exists / CREATE-if-not pattern.
# authentication_chain is managed durably via starrocksFESpec.config (fe.conf above);
# this Job only handles Security Integration DDL.
# Ref: https://docs.starrocks.io/docs/administration/user_privs/authentication/security_integration/
if sso_k8s_secret is not None:
    domain = starrocks_config.require("domain")
    fe_service = f"{stack_info.env_prefix}-starrocks-fe-service"

    setup_sh = """\
#!/bin/sh
set -e
if [ -z "$SSO_URL" ] || [ -z "$SSO_CLIENT_ID" ] || [ -z "$SSO_CLIENT_SECRET" ]; then
  echo "SSO credentials not yet available (VSO sync pending); retrying..."
  exit 1
fi
MC="mysql --default-auth=mysql_native_password -h $FE_SERVICE -P 9030 -u root"

if $MC -e 'SHOW SECURITY INTEGRATIONS' | grep -q keycloak; then
  $MC <<ENDSQL
ALTER SECURITY INTEGRATION keycloak SET(
  "auth_server_url" = "$SSO_URL/protocol/openid-connect/auth",
  "token_server_url" = "$SSO_URL/protocol/openid-connect/token",
  "client_id" = "$SSO_CLIENT_ID",
  "client_secret" = "$SSO_CLIENT_SECRET",
  "redirect_url" = "https://$DOMAIN/api/oauth2",
  "jwks_url" = "$SSO_URL/protocol/openid-connect/certs",
  "principal_field" = "preferred_username"
);
ENDSQL
else
  $MC <<ENDSQL
CREATE SECURITY INTEGRATION keycloak PROPERTIES (
  "type" = "authentication_oauth2",
  "auth_server_url" = "$SSO_URL/protocol/openid-connect/auth",
  "token_server_url" = "$SSO_URL/protocol/openid-connect/token",
  "client_id" = "$SSO_CLIENT_ID",
  "client_secret" = "$SSO_CLIENT_SECRET",
  "redirect_url" = "https://$DOMAIN/api/oauth2",
  "jwks_url" = "$SSO_URL/protocol/openid-connect/certs",
  "principal_field" = "preferred_username"
);
ENDSQL
fi
"""

    oauth2_setup_configmap_name = f"{stack_info.env_prefix}-starrocks-oauth2-setup"
    oauth2_setup_configmap = kubernetes.core.v1.ConfigMap(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-oauth2-setup-cm",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=oauth2_setup_configmap_name,
            namespace=namespace,
            labels=k8s_app_labels.model_dump(),
        ),
        data={"setup.sh": setup_sh},
    )

    kubernetes.batch.v1.Job(
        f"starrocks-{stack_info.env_prefix}-{stack_info.env_suffix}-oauth2-setup-job",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"starrocks-{stack_info.env_prefix}-oauth2-setup",
            namespace=namespace,
            labels=k8s_app_labels.model_dump(),
        ),
        spec=kubernetes.batch.v1.JobSpecArgs(
            active_deadline_seconds=300,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=k8s_app_labels.model_dump(),
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    init_containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="wait-for-fe",
                            image=cached_image_uri("mysql:8.0"),
                            command=[
                                "sh",
                                "-c",
                                "i=0; "
                                "until mysql"
                                " --default-auth=mysql_native_password"
                                " -h $FE_SERVICE -P 9030 -u root"
                                " -e 'SELECT 1' >/dev/null 2>&1; "
                                "do i=$((i+1)); "
                                "if [ $i -ge 36 ]; then "
                                "echo 'Timed out waiting for StarRocks FE';"
                                " exit 1; fi; "
                                "echo 'Waiting for StarRocks FE...'; "
                                "sleep 5; done",
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
                                kubernetes.core.v1.EnvVarArgs(
                                    name="FE_SERVICE",
                                    value=fe_service,
                                ),
                            ],
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="configure-oauth2",
                            image=cached_image_uri("mysql:8.0"),
                            command=["sh", "/scripts/setup.sh"],
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
                                kubernetes.core.v1.EnvVarArgs(
                                    name="FE_SERVICE",
                                    value=fe_service,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DOMAIN",
                                    value=domain,
                                ),
                            ],
                            env_from=[
                                kubernetes.core.v1.EnvFromSourceArgs(
                                    secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                        name=sso_k8s_secret_name,
                                        optional=True,
                                    ),
                                ),
                            ],
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="scripts",
                                    mount_path="/scripts",
                                    read_only=True,
                                ),
                            ],
                        ),
                    ],
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name="scripts",
                            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                                name=oauth2_setup_configmap_name,
                                default_mode=0o555,
                            ),
                        ),
                    ],
                    restart_policy="OnFailure",
                ),
            ),
        ),
        opts=ResourceOptions(
            depends_on=[
                starrocks_release,
                sso_k8s_secret,
                oauth2_setup_configmap,
            ],
            delete_before_replace=True,
        ),
    )
