"""ol-analytics-api in the local k3d cluster, from the deployed definition.

The same ``definition.*`` calls as src/ol_infrastructure/applications/
ol_analytics_api/__main__.py, with bindings for k3d: environment from
LOCAL_ENV_DEFAULTS with the ``env_vars`` of the developer's gitignored stack
file merged over it, a Secret with local values (secrets.yaml, plus the MITx
Online client pair mitxonline's program registers) for the variables deployed
stacks read from Vault, the image Tilt built (or the published one without a
checkout), and the Learn-scoped host's OIDC resources, shared plugins and route.
Not rendered: the canonical host and its bearer-only learner-records rule,
which the dashboard uses neither of. Tilt runs this through
local-dev/scripts/pulumi-app-apply.sh.
"""

import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions

from ol_infrastructure.applications.ol_analytics_api import definition
from ol_infrastructure.applications.ol_analytics_api.vault_secrets import (
    MITXONLINE_CLIENT_ID,
    MITXONLINE_CLIENT_SECRET,
    secret_env_names,
)
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCResources,
    OLApisixRoute,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.k8s import OLApplicationK8s
from ol_infrastructure.lib.local_dev_program import (
    MANAGED_BY_LABEL,
    ROOT_DOMAIN_DEFAULT,
    await_policy,
    ca_bundle_env,
    ca_bundle_mount,
    ca_bundle_volume,
    image_reference,
    k3d_provider,
    manifest,
    merged_env,
    secrets_hash,
    warn_leftover_app_env_local,
)
from ol_infrastructure.lib.ol_types import BusinessUnit, K8sGlobalLabels, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.secret_env import local_secret_data

# The app's Tilt directory (secrets.yaml), this program's parent.
APP_DIR = Path(__file__).resolve().parents[1]
# Named after the app, not the deployed ol-analytics: the core stack creates it
# only while the app is enabled, keyed on the app's name, and
# pulumi-app-apply.sh works in the namespace named after the app.
NAMESPACE = "ol-analytics-api"
SECRET_NAME = "ol-analytics-api-secrets"  # noqa: S105  # pragma: allowlist secret
ENV_SUFFIX = "dev"
# The deployed label set (environment comes out as the stack's "dev"), plus
# MANAGED_BY_LABEL. These keys join the Deployment's immutable selector, so
# changing any of them replaces it on every developer's cluster.
LABELS = (
    K8sGlobalLabels(
        ou=BusinessUnit.data, service=Services.ol_analytics_api, stack=parse_stack()
    ).model_dump()
    | MANAGED_BY_LABEL
)


def _mitxonline_inputs() -> ModuleType:
    """Load mitxonline's program inputs, which hold the client pair it registers.

    By path and under its own name: both programs run as ``__main__`` with their
    own directory on ``sys.path``, so ``import inputs`` would be ambiguous.
    """
    path = APP_DIR.parent / "mitxonline" / "pulumi" / "inputs.py"
    spec = importlib.util.spec_from_file_location("mitxonline_inputs", path)
    if spec is None or spec.loader is None:
        msg = f"cannot load {path}"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


root_domain = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", ROOT_DOMAIN_DEFAULT)
repository, tag = image_reference("mitodl/ol-analytics-api-app", os.environ)
# With a checkout, Tilt syncs src/ into the running container, so Granian reloads
# and the root filesystem stays writable. Without one nothing changes the code.
tilt_built = bool(os.environ.get("TILT_IMAGE_0"))
# Values for a developer's cluster, under the stack file's
# `ol_analytics_api:env_vars`, the key the deployed stacks' files use.
LOCAL_ENV_DEFAULTS = {
    # The data-platform lake's StarRocks. Deployed, the pod logs in to Vault for
    # dynamic credentials; with no OL_ANALYTICS_API_VAULT_* variables (the
    # bindings set no starrocks_vault) it reads these two instead
    # (core/db/vault_credentials.py). The allin1 image's root user has no
    # password, and main.py reads it with os.environ[...], so it is set, empty.
    "OL_ANALYTICS_API_STARROCKS_HOST": "starrocks.local-infra.svc.cluster.local",
    "STARROCKS_USER": "root",
    "STARROCKS_PASSWORD": "",
    # The org-manager check, against the local MITx Online through its gateway
    # host, as QA calls api.rc.mitxonline.mit.edu.
    "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_API_BASE_URL": (
        f"https://api.mitxonline.{root_domain}"
    ),
    # The ca_bundle_mount() below: public roots plus the mkcert root, which httpx
    # needs for the MITx Online and Keycloak calls.
    **ca_bundle_env(),
    "LOG_LEVEL": "INFO",
}
# The developer's gitignored stack file, Pulumi.Dev.yaml next to this program
# (pulumi-app-apply.sh creates it on the first apply; the .example lists the
# keys).
stack_config = pulumi.Config("ol_analytics_api")
env_vars, override_keys = merged_env(
    "ol_analytics_api:env_vars",
    LOCAL_ENV_DEFAULTS,
    stack_config.get_object("env_vars"),
    root_domain,
)
if override_keys:
    # Key names only, never values: the file may hold secrets.
    sys.stderr.write(
        f"ol_analytics_api:env_vars overrides: {', '.join(override_keys)}\n"
    )
warn_leftover_app_env_local(APP_DIR, "ol_analytics_api:env_vars")
# Which image runs is the one input a teammate cannot see from the files.
sys.stderr.write(f"image: {repository}:{tag}\n")

provider = k3d_provider()
opts = ResourceOptions(providers=[provider])

# Deployed stacks get these variables from Vault through VSO. Here one plain
# Secret carries the same names: the values secrets.yaml gives them, and the
# client pair mitxonline's pre-deploy Job registers while this app is enabled,
# taken from mitxonline's program so the two cannot drift.
mitxonline_inputs = _mitxonline_inputs()
mitxonline_client = {
    MITXONLINE_CLIENT_ID: mitxonline_inputs.ANALYTICS_API_CLIENT_ID,
    MITXONLINE_CLIENT_SECRET: mitxonline_inputs.ANALYTICS_API_CLIENT_SECRET,
}
local_secrets = manifest(APP_DIR / "secrets.yaml", root_domain)
secret_data = (
    local_secret_data(
        secret_env_names(),
        local_secrets["values"],
        local_secrets["unset"],
        supplied_elsewhere=mitxonline_client,
    )
    | mitxonline_client
)
secret = k8s.core.v1.Secret(
    SECRET_NAME,
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name=SECRET_NAME, namespace=NAMESPACE, labels=LABELS
    ),
    string_data=secret_data,
    opts=opts,
)

bindings = definition.OlAnalyticsApiBindings(
    env_suffix=ENV_SUFFIX,
    namespace=NAMESPACE,
    hostnames=definition.OlAnalyticsApiHostnames(
        canonical=None,
        learn=f"analytics.learn.{root_domain}",
    ),
    environment_variables=env_vars,
    # The MIT Learn frontend, as cors_frontend_origins names it in the deployed
    # stacks.
    cors_frontend_origins=[f"https://learn.{root_domain}"],
    k8s_labels=LABELS,
    secret_names=[SECRET_NAME],
    # The shared mit-learn client, as deployed (sso/mitlearn); apps-infra writes
    # this Secret into the namespace while the app is enabled.
    oidc_secret_name="ol-mitlearn-oidc",  # noqa: S106  # pragma: allowlist secret
    application_docker_tag=tag,
    cluster=definition.ClusterCapabilities(
        registry="direct", autoscalers=False, pod_monitors=False
    ),
    image_repository=repository,
    min_replicas=1,
    web=definition.WorkloadResources(
        requests={"cpu": "100m", "memory": "256Mi"},
        limits={"memory": stack_config.get("web_memory_limit") or "512Mi"},
    ),
    synced_source=tilt_built,
    extra_volumes=[ca_bundle_volume()],
    extra_volume_mounts=[ca_bundle_mount()],
)

config_hash = secrets_hash(secret_data)
app_config = definition.application_config(bindings).model_copy(
    # Env is in env:, so an env edit changes the pod template; a secrets.yaml
    # edit changes only the Secret, and the pod must roll for that too.
    update={"config_hash_inputs": {"secrets.yaml": config_hash}}
)

OLApplicationK8s(
    ol_app_k8s_config=app_config,
    opts=ResourceOptions(
        providers=[provider],
        depends_on=[secret],
        # No pre-deploy Job: the definition runs no migrations.
        transforms=[await_policy],
    ),
)

# The gateway objects for the Learn-scoped host, built as the deployed
# __main__.py builds them.
learn_oidc = OLApisixOIDCResources(
    f"ol-analytics-api-{ENV_SUFFIX}-learn-oidc-resources",
    oidc_config=definition.learn_oidc_config(bindings),
    opts=opts,
)
shared_plugins = OLApisixSharedPlugins(
    f"ol-analytics-api-{ENV_SUFFIX}-ol-shared-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name=definition.APPLICATION_NAME,
        resource_suffix="ol-shared-plugins",
        k8s_namespace=NAMESPACE,
        k8s_labels=LABELS,
        enable_defaults=True,
        # Local Alloy has no trace backend.
        enable_opentelemetry=False,
        cors_allow_origins=definition.cors_allow_origins(bindings),
    ),
    opts=opts,
)
OLApisixRoute(
    f"ol-analytics-api-{ENV_SUFFIX}-route",
    k8s_namespace=NAMESPACE,
    k8s_labels=LABELS,
    route_configs=[
        definition.learn_route_config(
            bindings,
            learn_oidc,
            shared_plugin_config_name=shared_plugins.resource_name,
        )
    ],
    opts=ResourceOptions(providers=[provider], delete_before_replace=True),
)
