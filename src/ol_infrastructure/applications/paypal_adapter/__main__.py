"""Run the Stripe-written PayPal adapter on the applications EKS cluster.

The adapter sits between Stripe Checkout, PayPal and the customer's browser for
every PayPal payment; our applications never call it. It is a stateless HTTP
service on port 8080 whose only configuration is a ``config.json`` read once at
startup. The decisions this stack encodes, and why, are in
docs/plans/paypal-adapter-eks-deployment.md.
"""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions

from bridge.lib.versions import PAYPAL_ADAPTER_IMAGE_DIGEST, PAYPAL_ADAPTER_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.services.apisix import (
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
    OLApisixUpstream,
    OLApisixUpstreamConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    Application,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import make_stack_reference, parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
adapter_config = Config("paypal_adapter")
vault_config = Config("vault")
setup_vault_provider(stack_info)

cluster_stack = make_stack_reference(projects.EKS, f"applications.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

APP_NAME = "paypal-adapter"
NAMESPACE = "paypal-adapter"
APP_PORT = 8080
# The vendor docs mount the config at this path (docs/01-deploy-and-manage-adapter.md).
CONFIG_DIR = "/run/secrets"
CONFIG_FILE = "config.json"
VAULT_MOUNT = "secret-paypal-adapter"
SECRET_KEYS = (
    "stripe_secret_key",
    "stripe_signing_key",
    "paypal_client_id",
    "paypal_client_secret",
)
# Stripe says it calls /paypal/payments from these addresses:
# https://stripe.com/files/ips/ips_webhooks.json (fetched 2026-10-30). The
# adapter also verifies Stripe's signature, so this is defence in depth. It
# fails closed if the list goes stale; compare it against the published list
# at each adapter upgrade.
STRIPE_WEBHOOK_IPS = [
    "3.18.12.63",
    "3.69.109.8",
    "3.120.168.93",
    "3.130.192.231",
    "13.235.14.237",
    "13.235.122.149",
    "18.211.135.69",
    "35.154.171.200",
    "35.157.207.129",
    "52.15.183.38",
    "54.88.130.119",
    "54.88.130.237",
    "54.187.174.169",
    "54.187.205.235",
    "54.187.216.72",
]

k8s_domain = adapter_config.require("k8s_domain")
livemode = adapter_config.require_bool("livemode")
apisix_ingress_class = adapter_config.get("apisix_ingress_class") or "apache-apisix"

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(NAMESPACE, ns)
)

k8s_global_labels = K8sAppLabels(
    application=Application.paypal_adapter,
    service=Services.paypal_adapter,
    product=Product.mitlearn,
    ou=BusinessUnit.mitx_online,
    source_repository="https://hub.docker.com/r/stripe/orchestration-adapters",
    stack=stack_info,
).model_dump()

##################################
# Secrets: SOPS -> Vault -> VSO  #
##################################

adapter_secrets = read_yaml_secrets(
    Path(f"paypal_adapter/secrets.{stack_info.env_suffix}.yaml")
)
vault_mount = vault.Mount(
    f"paypal-adapter-secrets-mount-{stack_info.env_suffix}",
    path=VAULT_MOUNT,
    type="kv-v2",
    options={"version": "2"},
    description="Stripe and PayPal credentials for the PayPal adapter.",
    opts=ResourceOptions(delete_before_replace=True),
)
vault_secrets = vault.generic.Secret(
    f"paypal-adapter-secrets-{stack_info.env_suffix}",
    path=vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps({key: adapter_secrets[key] for key in SECRET_KEYS}),
)

vault_policy = vault.Policy(
    f"paypal-adapter-vault-policy-{stack_info.env_suffix}",
    name=APP_NAME,
    policy=Path(__file__).parent.joinpath("paypal_adapter_policy.hcl").read_text(),
)
vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"paypal-adapter-vault-auth-backend-role-{stack_info.env_suffix}",
    role_name=APP_NAME,
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[NAMESPACE],
    token_policies=[vault_policy.name],
)
vault_k8s_resources = OLVaultK8SResources(
    resource_config=OLVaultK8SResourcesConfig(
        application_name=APP_NAME,
        namespace=NAMESPACE,
        labels=k8s_global_labels,
        vault_address=vault_config.require("address"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=vault_auth_backend_role.role_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[vault_auth_backend_role],
    ),
)

# VSO renders the whole config file from one template, so the pod sees the
# vendor's config.json and nothing of ours. The non-secret values are filled in
# here from stack config; the secret ones are Go-template lookups that VSO
# resolves against the Vault secret. Built through json.dumps on placeholders
# rather than as a literal, so the JSON is well formed and the template
# actions keep their unescaped quotes.
# Differences from the vendor template: "json" is true so Loki gets
# structured lines (flip back if the adapter's JSON does not parse in the
# existing pipeline). "level" stays at the vendor's "debug": Stripe asks for
# these logs when debugging and the volume is a few lines per payment.
config_template = json.dumps(
    {
        "adapter": {"base_url": f"https://{k8s_domain}", "livemode": livemode},
        "stripe": {"secret_key": "@@stripe_secret_key@@"},
        "logging": {"level": "debug", "json": True},
        "adapter_configs": {
            "paypal": {
                "client_id": "@@paypal_client_id@@",
                "client_secret": "@@paypal_client_secret@@",
                "stripe_signing_key": "@@stripe_signing_key@@",
                "enable_webhooks": True,
            }
        },
    },
    indent=2,
)
for key in SECRET_KEYS:
    config_template = config_template.replace(
        f"@@{key}@@", '{{ get .Secrets "' + key + '" }}'
    )

config_secret_name = "paypal-adapter-config"  # noqa: S105  # pragma: allowlist secret
# The adapter reads config.json once at startup, so the Deployment is a restart
# target: a rotated key in SOPS reaches the pods as a rolling restart with no
# manual step.
config_secret = OLVaultK8SSecret(
    name=f"paypal-adapter-config-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="paypal-adapter-config",
        namespace=NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_name=config_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount=VAULT_MOUNT,
        mount_type="kv-v2",
        path="secrets",
        templates={CONFIG_FILE: config_template},
        restart_targets=[
            OLVaultRestartTarget(kind="Deployment", name=f"{APP_NAME}-app")
        ],
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
        depends_on=[vault_secrets],
    ),
)

##################################
#        The adapter pods        #
##################################

health_probe = kubernetes.core.v1.HTTPGetActionArgs(
    path="/monitor/health", port=APP_PORT
)
# Readiness is tight so an unhealthy pod leaves the gateway in about 15s.
# Liveness is loose so a slow PayPal or Stripe call does not restart a pod.
# The startup probe covers a slow image pull or the first config fetch.
probe_configs = {
    "readiness_probe": kubernetes.core.v1.ProbeArgs(
        http_get=health_probe,
        period_seconds=5,
        failure_threshold=3,
    ),
    "liveness_probe": kubernetes.core.v1.ProbeArgs(
        http_get=health_probe,
        period_seconds=10,
        failure_threshold=6,
    ),
    "startup_probe": kubernetes.core.v1.ProbeArgs(
        http_get=health_probe,
        period_seconds=5,
        failure_threshold=30,
    ),
}

adapter_k8s_config = OLApplicationK8sConfig(
    project_root=Path(__file__).parent,
    application_config={"CONFIG_PATH": f"{CONFIG_DIR}/{CONFIG_FILE}"},
    application_name=APP_NAME,
    application_namespace=NAMESPACE,
    application_lb_service_name=APP_NAME,
    application_lb_service_port_name="http",
    # Vendor stack runs 3-10 tasks; downtime fails PayPal checkouts and
    # renewals, and Stripe may switch PayPal off at checkout until it recovers.
    application_min_replicas=3,
    application_max_replicas=6,
    application_deployment_use_anti_affinity=True,
    app_pdb_maximum_unavailable=1,
    k8s_global_labels=k8s_global_labels,
    webapp_alert_tier=(
        AlertTier.page if stack_info.env_suffix == "production" else AlertTier.notify
    ),
    # The adapter takes a file, not environment variables.
    env_from_secret_names=[],
    application_image_repository="stripe/orchestration-adapters",
    application_image_digest=PAYPAL_ADAPTER_IMAGE_DIGEST,
    application_version=PAYPAL_ADAPTER_VERSION,
    application_port=APP_PORT,
    registry="dockerhub",
    # Not a Django app: no nginx sidecar, no migrations, no collectstatic.
    import_nginx_config=False,
    init_migrations=False,
    init_collectstatic=False,
    resource_requests={"cpu": "250m", "memory": "512Mi"},
    resource_limits={"memory": "512Mi"},
    probe_configs=probe_configs,
    # The component's PodMonitor is for the Granian metrics port; the adapter
    # serves metrics on the application port, so it is written below.
    manage_pod_monitor=False,
    extra_volumes=[
        kubernetes.core.v1.VolumeArgs(
            name="adapter-config",
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(
                secret_name=config_secret_name,
            ),
        ),
    ],
    extra_volume_mounts=[
        kubernetes.core.v1.VolumeMountArgs(
            name="adapter-config",
            mount_path=CONFIG_DIR,
            read_only=True,
        ),
    ],
)
adapter_k8s_app = OLApplicationK8s(
    ol_app_k8s_config=adapter_k8s_config,
    opts=ResourceOptions(depends_on=[config_secret]),
)

# The webapp container port is unnamed, so the endpoint is selected by number.
kubernetes.apiextensions.CustomResource(
    f"paypal-adapter-pod-monitor-{stack_info.env_suffix}",
    api_version="monitoring.coreos.com/v1",
    kind="PodMonitor",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=APP_NAME,
        namespace=NAMESPACE,
        labels=k8s_global_labels,
    ),
    spec={
        "selector": {
            "matchLabels": {
                "ol.mit.edu/application": APP_NAME,
                "ol.mit.edu/component": "webapp",
            }
        },
        "namespaceSelector": {"matchNames": [NAMESPACE]},
        "podMetricsEndpoints": [
            {
                "portNumber": APP_PORT,
                "path": "/monitor/metrics",
                "scheme": "http",
                "interval": "30s",
            }
        ],
    },
    opts=ResourceOptions(depends_on=[adapter_k8s_app]),
)

##################################
#            Ingress             #
##################################

tls_secret_name = "paypal-adapter-tls-pair"  # noqa: S105  # pragma: allowlist secret
OLCertManagerCert(
    f"paypal-adapter-tls-cert-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name=APP_NAME,
        k8s_namespace=NAMESPACE,
        k8s_labels=k8s_global_labels,
        create_apisixtls_resource=True,
        apisixtls_ingress_class=apisix_ingress_class,
        dest_secret_name=tls_secret_name,
        dns_names=[k8s_domain],
    ),
)

OLApisixUpstream(
    name=f"paypal-adapter-apisix-upstream-{stack_info.env_suffix}",
    upstream_config=OLApisixUpstreamConfig(
        service_name=APP_NAME,
        k8s_namespace=NAMESPACE,
        k8s_labels=k8s_global_labels,
    ),
)

shared_plugins = OLApisixSharedPlugins(
    name=f"paypal-adapter-apisix-shared-plugins-{stack_info.env_suffix}",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name=APP_NAME,
        resource_suffix="shared-plugins",
        k8s_namespace=NAMESPACE,
        k8s_labels=k8s_global_labels,
        # Stripe and PayPal call server-to-server and the customer's browser
        # only follows redirects; nothing fetches these endpoints with XHR.
        cors_allow_origins=[],
        enable_defaults=True,
    ),
)


# Four paths and nothing else. /monitor/* has no route, so the gateway answers
# 404 for it and only the probes and the PodMonitor reach it, on the pod.
# OLApisixRouteConfig matches on host and path only; the adapter answers 404
# or 405 for a wrong method.
def adapter_route(
    route_name: str,
    paths: list[str],
    plugins: list[OLApisixPluginConfig] | None = None,
) -> OLApisixRouteConfig:
    """Route one set of paths on the adapter host to the adapter Service."""
    return OLApisixRouteConfig(
        route_name=route_name,
        priority=10,
        shared_plugin_config_name=shared_plugins.resource_name,
        plugins=list(plugins or []),
        hosts=[k8s_domain],
        paths=paths,
        backend_service_name=APP_NAME,
        backend_service_port=APP_PORT,
        # Resolve to pod IPs so a terminating or unready pod leaves the pool
        # instead of staying pinned behind a keepalive to the ClusterIP.
        backend_resolve_granularity="endpoint",
    )


OLApisixRoute(
    name=f"paypal-adapter-apisix-route-{stack_info.env_suffix}",
    k8s_namespace=NAMESPACE,
    k8s_labels=k8s_global_labels,
    ingress_class_name=apisix_ingress_class,
    route_configs=[
        # Stripe creates, confirms, saves and refunds PayPal orders here.
        adapter_route(
            "paypal-adapter-payments",
            ["/paypal/payments"],
            plugins=[
                OLApisixPluginConfig(
                    name="ip-restriction",
                    config={"whitelist": STRIPE_WEBHOOK_IPS},
                ),
            ],
        ),
        # PayPal's vault-token webhooks, and the customer's browser on its way
        # back from PayPal to Stripe Checkout. PayPal publishes no stable
        # source range; the adapter verifies its signature.
        adapter_route(
            "paypal-adapter-public",
            ["/paypal/webhook", "/paypal/approved", "/paypal/canceled"],
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[adapter_k8s_app],
    ),
)
