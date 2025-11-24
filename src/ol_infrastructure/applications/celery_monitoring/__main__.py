import json
import re
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import Config, InvokeOptions, Output, StackReference

from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
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
from ol_infrastructure.lib.heroku import get_heroku_provider
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)
celery_monitoring_config = Config("celery_monitoring")
opensearch_stack = StackReference(
    f"infrastructure.aws.opensearch.celery_monitoring.{stack_info.name}"
)
vault_mount_stack = StackReference(
    f"substructure.vault.static_mounts.operations.{stack_info.name}"
)
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
operations_vpc = network_stack.require_output("operations_vpc")

# K8s stack references
cluster_stack = StackReference(f"infrastructure.aws.eks.operations.{stack_info.name}")
cluster_substructure_stack = StackReference(
    f"substructure.aws.eks.operations.{stack_info.name}"
)
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))


def build_broker_subscriptions(
    project_outputs: list[tuple[str, Output]],
) -> str:
    """Create a dict of Redis cache configs for each edxapp stack"""
    broker_subs = []

    def stack_to_app(stack):
        return re.sub(r"[^a-zA-Z]", "", "".join(stack.split(".")[1:-1]))

    for stack, project_output in project_outputs:
        app_name = stack_to_app(stack)
        if app_name.endswith("mitx"):
            app_name += "live"
        broker_subs.append(
            {
                "broker": f"rediss://default:{project_output['redis_token']}@{project_output['redis']}:6379/1?ssl_cert_reqs=required",
                "broker_management_url": None,
                "exchange": "celeryev",
                "queue": "leek.fanout",
                "routing_key": "#",
                "org_name": "MIT Open Learning Engineering",
                "app_name": app_name[:15],
                "app_env": stack_info.env_suffix,
            }
        )

    heroku_app_map = celery_monitoring_config.require_object("heroku_map")
    for heroku_owner, app_list in heroku_app_map.items():
        heroku_provider = get_heroku_provider(heroku_owner)
        for app in app_list:
            heroku_app = heroku.app.get_app(
                name=app, opts=InvokeOptions(provider=heroku_provider)
            )
            broker_subs.append(
                {
                    "broker": f"{heroku_app.config_vars['REDISCLOUD_URL']}/0",
                    "broker_management_url": None,
                    "exchange": "celeryev",
                    "queue": "leek.fanout",
                    "routing_key": "#",
                    "org_name": "MIT Open Learning Engineering",
                    "app_name": f"heroku{app.replace('-', '')}"[:15],
                    "app_env": stack_info.env_suffix,
                }
            )
    arbitrary_dict = {"broker_subscriptions": broker_subs}
    return json.dumps(arbitrary_dict)


stacks = [
    f"applications.edxapp.xpro.{stack_info.name}",
    f"applications.edxapp.mitx.{stack_info.name}",
    f"applications.edxapp.mitx-staging.{stack_info.name}",
    f"applications.edxapp.mitxonline.{stack_info.name}",
    f"applications.superset.{stack_info.name}",
    f"applications.mitxonline.{stack_info.name}",
    f"applications.mit_learn.{stack_info.name}",
    f"applications.learn_ai.{stack_info.name}",
]

redis_outputs: list[tuple[str, Output]] = []
for stack in stacks:
    project = stack.split(".")[1]
    redis_outputs.append((stack, StackReference(stack).require_output(project)))
redis_broker_subscriptions = Output.all(*redis_outputs).apply(
    build_broker_subscriptions
)

celery_monitoring_vault_kv_path = vault_mount_stack.require_output(
    "celery_monitoring_kv"
)["path"]

vault.kv.SecretV2(
    "celery-monitoring-vault-secret-redis-brokers",
    mount=celery_monitoring_vault_kv_path,
    name="redis_brokers",
    data_json=redis_broker_subscriptions,
)

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"operations-{stack_info.env_suffix}"}
)

celery_monitoring_domain = celery_monitoring_config.require("domain")

# Kubernetes deployment
celery_monitoring_namespace = "operations"

# Verify namespace exists in cluster
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(celery_monitoring_namespace, ns)
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.celery_monitoring,
    ou=BusinessUnit.operations,
    stack=stack_info,
)

application_labels = {
    **k8s_global_labels.model_dump(),
    "app": "celery-monitoring",
}

# Get OpenSearch endpoint for Leek configuration
opensearch_endpoint = opensearch_stack.require_output("cluster")["endpoint"]

# IAM policy for IRSA (if needed for AWS service access)
celery_monitoring_iam_policy_document = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "ec2:DescribeInstances",
                "ec2:DescribeTags",
            ],
            "Resource": "*",
        }
    ],
}

# OLEKSAuthBinding for IRSA and Vault K8s auth
celery_monitoring_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="celery-monitoring",
        namespace=celery_monitoring_namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=celery_monitoring_iam_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath(
            "celery_monitoring_server_policy.hcl"
        ),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="celery-monitoring",
        vault_sync_service_account_names=["celery-monitoring-vault"],
        k8s_labels=k8s_global_labels,
    )
)

# Create broker subscriptions secret via Vault Secrets Operator
leek_broker_subscriptions_secret = OLVaultK8SSecret(
    f"celery-monitoring-broker-subscriptions-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        dest_secret_labels=application_labels,
        dest_secret_name="leek-broker-subscriptions",  # pragma: allowlist-secret # noqa: S106, E501
        exclude_raw=True,
        excludes=[".*"],
        labels=application_labels,
        mount=celery_monitoring_vault_kv_path,
        mount_type="kv-v2",
        name="leek-broker-subscriptions",
        namespace=celery_monitoring_namespace,
        path="redis_brokers",
        refresh_after="1m",
        templates={
            "LEEK_AGENT_SUBSCRIPTIONS": '{{ get .Secrets "broker_subscriptions" | toJson }}',  # noqa: E501
        },
        vaultauth=celery_monitoring_auth_binding.vault_k8s_resources.auth_name,
    ),
)

cert_manager_certificate = OLCertManagerCert(
    f"celery-monitoring-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="celery-monitoring",
        k8s_namespace=celery_monitoring_namespace,
        k8s_labels=application_labels,
        create_apisixtls_resource=True,
        dest_secret_name="celery-monitoring-tls",  # pragma: allowlist-secret  # noqa: E501, S106
        dns_names=[celery_monitoring_domain],
    ),
)


# OIDC authentication resources (using existing secret-operations/sso/leek)
celery_monitoring_oidc_resources = OLApisixOIDCResources(
    f"celery-monitoring-oidc-resources-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="celery-monitoring",
        k8s_labels=application_labels,
        k8s_namespace=celery_monitoring_namespace,
        oidc_logout_path="/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{celery_monitoring_domain}/",
        oidc_session_cookie_lifetime=60 * 20160,  # 2 weeks
        oidc_use_session_secret=True,
        oidc_scope="openid profile email",
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/leek",
        vaultauth=celery_monitoring_auth_binding.vault_k8s_resources.auth_name,
    ),
)

# Leek container image from ECR mirror
leek_image = cached_image_uri("kodhive/leek")

# Kubernetes Deployment for Leek
leek_deployment = kubernetes.apps.v1.Deployment(
    f"celery-monitoring-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="celery-monitoring",
        namespace=celery_monitoring_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=celery_monitoring_config.get_int("replicas") or 1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": "celery-monitoring"},
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=application_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name=celery_monitoring_auth_binding.vault_k8s_resources.service_account_name,
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="leek",
                        image=leek_image,
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=5000,
                                name="http",
                            ),
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_API_LOG_LEVEL",
                                value=celery_monitoring_config.get("log_level")
                                or "INFO",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_API_ENABLE_AUTH",
                                value="false",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_WEB_URL",
                                value=f"https://{celery_monitoring_domain}",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_API_URL",
                                value=f"https://{celery_monitoring_domain}/",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_AGENT_API_SECRET",
                                value="not-secret",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_AGENT_LOG_LEVEL",
                                value="INFO",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_ENABLE_API", value="true"
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_ENABLE_AGENT", value="true"
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_ENABLE_WEB", value="true"
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="LEEK_ES_URL",
                                value=opensearch_endpoint.apply(
                                    lambda ep: f"https://{ep}"
                                ),
                            ),
                        ],
                        env_from=[
                            kubernetes.core.v1.EnvFromSourceArgs(
                                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                    name="leek-broker-subscriptions",
                                ),
                            ),
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": celery_monitoring_config.get("cpu_request")
                                or "500m",
                                "memory": celery_monitoring_config.get("memory_request")
                                or "512Mi",
                            },
                            limits={
                                "cpu": celery_monitoring_config.get("cpu_limit")
                                or "1000m",
                                "memory": celery_monitoring_config.get("memory_limit")
                                or "1Gi",
                            },
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/v1/manage/hc",
                                port=5000,
                            ),
                            initial_delay_seconds=30,
                            period_seconds=10,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/v1/manage/hc",
                                port=5000,
                            ),
                            initial_delay_seconds=10,
                            period_seconds=5,
                        ),
                        startup_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/v1/manage/hc",
                                port=5000,
                            ),
                            initial_delay_seconds=10,
                            period_seconds=10,
                            failure_threshold=6,
                            success_threshold=1,
                            timeout_seconds=5,
                        ),
                    ),
                ],
            ),
        ),
    ),
)

# Kubernetes Service for Leek
leek_service = kubernetes.core.v1.Service(
    f"celery-monitoring-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="celery-monitoring",
        namespace=celery_monitoring_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector=application_labels,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                port=5000,
                target_port=5000,
                name="api",
            ),
            kubernetes.core.v1.ServicePortArgs(
                port=8000,
                target_port=8000,
                name="web",
            ),
        ],
    ),
)

# Get OIDC plugin config as dict and wrap in OLApisixPluginConfig
oidc_plugin = OLApisixPluginConfig(
    **celery_monitoring_oidc_resources.get_full_oidc_plugin_config(unauth_action="auth")
)

leek_apisix_route = OLApisixRoute(
    name=f"celery-monitoring-apisix-route-{stack_info.env_suffix}",
    k8s_namespace=celery_monitoring_namespace,
    k8s_labels=application_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="web",
            priority=10,
            plugins=[
                oidc_plugin,
            ],
            hosts=[celery_monitoring_domain],
            paths=["/*"],
            backend_service_name="celery-monitoring",
            backend_service_port=8000,
        ),
        OLApisixRouteConfig(
            route_name="api",
            priority=0,
            plugins=[
                oidc_plugin,
            ],
            hosts=[celery_monitoring_domain],
            paths=["/v1/*"],
            backend_service_name="celery-monitoring",
            backend_service_port=5000,
        ),
    ],
)


# DNS is managed by external-dns via annotations on the APISIX service
# Domain must be added to eks:apisix_domains in the EKS stack configuration
# (infrastructure.aws.eks.operations.{env})
