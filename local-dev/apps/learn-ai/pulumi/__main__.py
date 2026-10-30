"""learn-ai in the local k3d cluster, from the deployed definition.

The same ``definition.*`` calls as src/ol_infrastructure/applications/learn_ai/
__main__.py, with bindings for k3d: environment from LOCAL_ENV_DEFAULTS with
the ``env_vars`` of the developer's gitignored stack file merged over it, a
Secret with local values (secrets.yaml) for variables deployed stacks read from
Vault, the image Tilt built (or the published one without a checkout),
laptop sizing with the memory limits from that stack file, a pre-deploy Job
that migrates and creates the cache tables, and the gateway's OIDC resources,
shared plugins, routes and canvas-agent consumer. Tilt runs this through
local-dev/scripts/pulumi-app-apply.sh.
"""

import os
import sys
from functools import partial
from pathlib import Path

import pulumi
import pulumi_kubernetes as k8s
from pulumi import ResourceOptions
from pulumi_kubernetes.meta.v1 import ObjectMetaArgs

from ol_infrastructure.applications.learn_ai import definition
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

# The app's Tilt directory (secrets.yaml), this program's parent.
APP_DIR = Path(__file__).resolve().parents[1]
SECRET_NAME = "learn-ai-secrets"  # noqa: S105  # pragma: allowlist secret
ENV_SUFFIX = "dev"
IMAGE_REPOSITORY = "mitodl/learn-ai-app"
# The deployed label set (environment comes out as the stack's "dev"), plus
# MANAGED_BY_LABEL. These keys join every Deployment's immutable selector, so
# changing any of them replaces every Deployment on every developer's cluster.
LABELS = (
    K8sGlobalLabels(
        ou=BusinessUnit.mit_learn, service=Services.mit_learn, stack=parse_stack()
    ).model_dump()
    | MANAGED_BY_LABEL
)

root_domain = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", ROOT_DOMAIN_DEFAULT)
repository, tag = image_reference(IMAGE_REPOSITORY, os.environ)
# Values for a developer's cluster, applied over definition.LOCAL_AND_DEPLOYED_ENV
# and under the stack file's `learn_ai:env_vars`; `env_vars` means the same in
# the deployed stacks' files.
LOCAL_ENV_DEFAULTS = {
    "DJANGO_SETTINGS_MODULE": "main.settings",
    "MITOL_APP_BASE_URL": f"https://ai.learn.{root_domain}",
    "MITOL_DB_DISABLE_SSL": "True",
    "MITOL_USE_S3": "False",
    "USE_X_FORWARDED_HOST": "True",
    "USE_X_FORWARDED_PORT": "True",
    # The ca_bundle_mount() below: public roots plus the mkcert root, so
    # inter-pod HTTPS to the local TLS endpoints verifies.
    **ca_bundle_env(),
    # Keycloak/OIDC
    "KEYCLOAK_BASE_URL": f"https://sso.ol.{root_domain}",
    "KEYCLOAK_REALM_NAME": "olapps",
    # Valkey: this app's logical DBs are 8 and 9. Each app needs its own, or
    # Celery and RedBeat pick up another app's tasks; the allocation is listed
    # under Shared Valkey in local-dev/ARCHITECTURE.md.
    "REDIS_URL": "redis://valkey.local-infra.svc.cluster.local:6379/8",
    "REDIS_DOMAIN": "redis://valkey.local-infra.svc.cluster.local:6379/8",
    "CELERY_BROKER_URL": "redis://valkey.local-infra.svc.cluster.local:6379/9",
    # mit-learn API (for AI to call back)
    "MIT_LEARN_API_PROXY_BASE_URL": (
        "http://mitlearn-webapp.mit-learn.svc.cluster.local:8060/api/"
    ),
    # Email (Mailpit); settings reads the MITOL_EMAIL_* prefix
    "MITOL_EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
    "MITOL_EMAIL_HOST": "mailpit.local-infra.svc.cluster.local",
    "MITOL_EMAIL_PORT": "1025",
    "MITOL_EMAIL_TLS": "False",
    # settings.py requires the Mailgun settings unconditionally, even when
    # sending through SMTP. Stub values: Mailpit handles all email locally.
    # MAILGUN_KEY is in secrets.yaml.
    "MAILGUN_SENDER_DOMAIN": f"ol.{root_domain}",
    "MAILGUN_FROM_EMAIL": f"MIT Learn AI <noreply@ol.{root_domain}>",
    "MITOL_COOKIE_NAME": "learn-ai-auth",
    "MITOL_COOKIE_DOMAIN": f".{root_domain}",
    # learn-ai is also served at api.learn.<root>/ai/*, the host where mit-learn
    # sets sessionid/csrftoken on .learn.<root>. Own names keep the two apps'
    # Django cookies apart, as in RC and production. The CSRF cookie sits on
    # .learn.<root> so the frontend on learn.<root> can read it.
    "SESSION_COOKIE_NAME": "session_learn_ai",
    "CSRF_COOKIE_NAME": "learn_ai_csrftoken",
    "CSRF_COOKIE_DOMAIN": f".learn.{root_domain}",
    "CSRF_TRUSTED_ORIGINS": (
        f'["https://ai.learn.{root_domain}","https://learn.{root_domain}",'
        f'"https://api.learn.{root_domain}","https://ai-sandbox.learn.{root_domain}"]'
    ),
    # Hosts /http/login/?next= may return to; the setting's default is the
    # production hosts. The local counterparts of QA's list (sandbox, Learn, Open
    # edX, MITx Online), plus ai.learn itself.
    "LOGIN_REDIRECT_ALLOWED_HOSTS": (
        f'["ai.learn.{root_domain}","ai-sandbox.learn.{root_domain}",'
        f'"learn.{root_domain}","lms.{root_domain}","mitxonline.{root_domain}"]'
    ),
}
# The developer's gitignored stack file, Pulumi.Dev.yaml next to this program
# (pulumi-app-apply.sh creates it on the first apply; the .example lists the
# keys). `env_vars` is the deployed stacks' key; the four memory limits are
# local keys in the same style.
stack_config = pulumi.Config("learn_ai")
env_vars, override_keys = merged_env(
    "learn_ai:env_vars",
    LOCAL_ENV_DEFAULTS,
    stack_config.get_object("env_vars"),
    root_domain,
)
if override_keys:
    # Key names only, never values: the file may hold secrets.
    sys.stderr.write(f"learn_ai:env_vars overrides: {', '.join(override_keys)}\n")
warn_leftover_app_env_local(APP_DIR, "learn_ai:env_vars")
# Which image runs is the one input a teammate cannot see from the files.
sys.stderr.write(f"image: {repository}:{tag}\n")

provider = k3d_provider()
opts = ResourceOptions(providers=[provider])

# Deployed stacks read their secret variables from Vault through VSO. Here one
# plain Secret carries the ones local-dev needs, with the values secrets.yaml
# gives them.
local_secrets = manifest(APP_DIR / "secrets.yaml", root_domain)
secret_data = local_secrets["values"]
non_strings = sorted(
    key for key, value in secret_data.items() if not isinstance(value, str)
)
if non_strings:
    msg = f"secrets.yaml values must be strings (quote them): {non_strings}"
    raise ValueError(msg)
# The canvas consumer's key is the gateway's, not a pod variable, so it stays out
# of the Secret.
canvas_key = local_secrets["canvas_syllabus_token"]
secret = k8s.core.v1.Secret(
    SECRET_NAME,
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name=SECRET_NAME, namespace=definition.NAMESPACE, labels=LABELS
    ),
    string_data=secret_data,
    opts=opts,
)


# Laptop-sized; the definition's defaults are sized for production nodes. The
# memory limit of each workload is stack config; the requests stay as written
# here. A web_memory_limit under 1335Mi fails: Granian's derived RSS cap,
# 0.9 * limit less the definition's GRANIAN_WORKER_STARTUP_RSS, would sit at or
# below that startup RSS and respawn the worker continuously.
def _workload(cpu: str, memory: str, limit_key: str, limit: str):
    return definition.WorkloadResources(
        requests={"cpu": cpu, "memory": memory},
        limits={"memory": stack_config.get(limit_key) or limit},
    )


sizing = definition.LearnAiSizing(
    web=_workload("100m", "512Mi", "web_memory_limit", "2Gi"),
    celery_beat=_workload("10m", "256Mi", "beat_memory_limit", "1Gi"),
    # One merged worker locally; the definition sizes it from the largest queue.
    celery_queues=dict.fromkeys(
        definition.CELERY_QUEUE_NAMES,
        _workload("50m", "512Mi", "worker_memory_limit", "1536Mi"),
    ),
)
pre_deploy = _workload("100m", "512Mi", "pre_deploy_memory_limit", "1Gi")

bindings = definition.LearnAiBindings(
    env_suffix=ENV_SUFFIX,
    hostnames=definition.LearnAiHostnames(
        api=f"ai.learn.{root_domain}",
        frontend=f"ai-sandbox.learn.{root_domain}",
        learn_api=f"api.learn.{root_domain}",
    ),
    environment_variables=env_vars,
    cors_frontend_origins=[
        f"https://learn.{root_domain}",
        f"https://lms.{root_domain}",
    ],
    k8s_labels=LABELS,
    secret_names=[SECRET_NAME],
    # The shared mit-learn client, as deployed (sso/mitlearn); apps-infra writes
    # this Secret into the learn-ai namespace.
    oidc_secret_name="ol-mitlearn-oidc",  # noqa: S106  # pragma: allowlist secret
    application_docker_tag=tag,
    cluster=definition.ClusterCapabilities(
        registry="direct", autoscalers=False, pod_monitors=False
    ),
    image_repository=repository,
    min_replicas=1,
    sizing=sizing,
    celery_topology="merged",
    # Bindings here, not a model_copy: the deployed program needs these fields
    # for its Azure token mount, so the definition passes them through.
    extra_volumes=[ca_bundle_volume()],
    extra_volume_mounts=[ca_bundle_mount()],
)

config_hash = secrets_hash(secret_data)
app_config = definition.application_config(bindings).model_copy(
    update={
        # The definition creates no cache tables (see its init_collectstatic
        # comment). The Job creates them before every Deployment starts, because
        # features.is_enabled reads the durable cache in every process.
        "pre_deploy_commands": [
            (
                "migrate",
                [
                    "bash",
                    "-c",
                    "python manage.py migrate --noinput"
                    " && python manage.py createcachetable",
                ],
            )
        ],
        "pre_deploy_resource_requests": pre_deploy.requests,
        "pre_deploy_resource_limits": pre_deploy.limits,
        # Env is in env:, so an env edit changes the pod template; a secrets.yaml
        # edit changes only the Secret, and pods must roll for that too.
        "config_hash_inputs": {"secrets.yaml": config_hash},
    }
)

OLApplicationK8s(
    ol_app_k8s_config=app_config,
    opts=ResourceOptions(
        providers=[provider],
        depends_on=[secret],
        # The same hash on the Job's pod template: a secrets.yaml edit
        # (DATABASE_URL, say) then replaces the Job and re-runs migrate, which
        # the Deployments' pod-template hash alone would not.
        transforms=[
            partial(
                await_policy, job_annotations={"ol.mit.edu/config-hash": config_hash}
            )
        ],
    ),
)

# The gateway objects, built as the deployed __main__.py builds them. Both route
# groups read the shared mit-learn client's Secret (bindings.oidc_secret_name).
direct_oidc = OLApisixOIDCResources(
    f"learn-ai-{ENV_SUFFIX}-oidc-resources",
    oidc_config=definition.direct_oidc_config(bindings),
    opts=opts,
)
prefixed_oidc = OLApisixOIDCResources(
    f"learn-ai-mit-learn-{ENV_SUFFIX}-oidc-resources",
    oidc_config=definition.prefixed_oidc_config(bindings),
    opts=opts,
)
shared_plugins = OLApisixSharedPlugins(
    f"learn-ai-{ENV_SUFFIX}-ol-shared-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="learn-ai",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=definition.NAMESPACE,
        k8s_labels=LABELS,
        enable_defaults=True,
        # Local Alloy has no trace backend.
        enable_opentelemetry=False,
        cors_allow_origins=definition.cors_allow_origins(bindings),
        plugins=list(definition.shared_plugins(bindings)),
    ),
    opts=opts,
)
OLApisixRoute(
    f"learn-ai-{ENV_SUFFIX}-https-olapisixroute",
    k8s_namespace=definition.NAMESPACE,
    k8s_labels=LABELS,
    route_configs=definition.direct_route_configs(
        bindings,
        direct_oidc,
        shared_plugin_config_name=shared_plugins.resource_name,
    ),
    opts=ResourceOptions(providers=[provider], delete_before_replace=True),
)
OLApisixRoute(
    f"mit-learn-learn-ai-{ENV_SUFFIX}-https-olapisixroute",
    k8s_namespace=definition.NAMESPACE,
    k8s_labels=LABELS,
    route_configs=definition.prefixed_route_configs(
        bindings,
        prefixed_oidc,
        shared_plugin_config_name=shared_plugins.resource_name,
    ),
    opts=ResourceOptions(providers=[provider], delete_before_replace=True),
)
# The key-auth consumer for the /http/canvas_* routes, with the key from
# secrets.yaml.
k8s.apiextensions.CustomResource(
    f"learn-ai-{ENV_SUFFIX}-https-apisix-consumer",
    api_version="apisix.apache.org/v2",
    kind="ApisixConsumer",
    metadata=ObjectMetaArgs(
        name=definition.CANVAS_AGENT_CONSUMER_NAME,
        namespace=definition.NAMESPACE,
        labels=LABELS,
    ),
    spec=definition.canvas_agent_consumer_spec(pulumi.Output.secret(canvas_key)),
    opts=opts,
)
