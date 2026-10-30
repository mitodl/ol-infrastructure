"""MITx Online in the local k3d cluster, from the deployed definition.

The same ``definition.*`` calls as src/ol_infrastructure/applications/mitxonline/
__main__.py, with bindings for k3d: environment from LOCAL_ENV_DEFAULTS with
the ``vars`` of the developer's gitignored stack file merged over it, a
Secret holding the deployed Vault-backed variables (local values from
secrets.yaml), the image Tilt built (or the published one without a checkout),
laptop sizing with the memory limits from that stack file, and the
``ol-mitlearn-oidc`` Secret the apps-infra stack already wrote for the gateway
routes. Tilt runs this through local-dev/scripts/pulumi-app-apply.sh.
"""

import hashlib
import json
import os
import sys
from functools import partial
from pathlib import Path

import pulumi
import pulumi_kubernetes as k8s
import yaml
from inputs import (
    ROOT_DOMAIN_DEFAULT,
    await_policy,
    image_reference,
    manifest,
    merged_env,
    with_local_steps,
)
from pulumi import ResourceOptions

from ol_infrastructure.applications.mitxonline import definition
from ol_infrastructure.applications.mitxonline.vault_secrets import secret_env_names
from ol_infrastructure.components.services.apisix import (
    OLApisixExternalUpstream,
    OLApisixOIDCResources,
    OLApisixRoute,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.k8s import OLApplicationK8s
from ol_infrastructure.lib.ol_types import (
    Application,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.secret_env import local_secret_data

# The app's Tilt directory (secrets.yaml), this program's parent.
APP_DIR = Path(__file__).resolve().parents[1]
SECRET_NAME = "mitxonline-secrets"  # noqa: S105  # pragma: allowlist secret
ENV_SUFFIX = "dev"
# Public roots plus the mkcert root, so pods trust both public endpoints and
# local TLS. The core stack writes this ConfigMap into every app namespace
# (CA_BUNDLE_CONFIGMAP in local-dev/infra/modules/tls.py).
CA_BUNDLE_CONFIGMAP = "local-dev-ca-bundle"
CA_BUNDLE_DIR = "/etc/ssl/local-dev"
# The deployed label set (environment comes out as the stack's "dev"), plus the
# key pulumi-app-apply.sh selects the applied objects by. These keys join every
# Deployment's immutable selector, so changing any of them replaces every
# Deployment on every developer's cluster.
LABELS = K8sAppLabels(
    application=Application.mitxonline,
    product=Product.mitlearn,
    service=Services.mitxonline,
    ou=BusinessUnit.mitx_online,
    source_repository="https://github.com/mitodl/mitxonline",
    stack=parse_stack(),
).model_dump() | {"ol.mit.edu/managed-by": "local-dev-pulumi"}

root_domain = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", ROOT_DOMAIN_DEFAULT)
repository, tag = image_reference(os.environ)
# Only the image Tilt builds (the Dockerfile's local-dev stage, dev deps included)
# carries granian[reload]; with --reload the published image exits at startup.
tilt_built = bool(os.environ.get("TILT_IMAGE_0"))
# Values for a developer's cluster, applied over definition.LOCAL_AND_DEPLOYED_ENV
# and under the stack file's `mitxonline:vars`; the local analogue of the
# deployed program's DEPLOYED_ENV_DEFAULTS, with `vars` meaning the same in both.
LOCAL_ENV_DEFAULTS = {
    "DJANGO_SETTINGS_MODULE": "main.settings",
    "MITX_ONLINE_BASE_URL": f"https://mitxonline.{root_domain}",
    "MITXONLINE_DOCKER_BASE_URL": f"https://mitxonline.{root_domain}",
    # Redirect target for a first-time login (GatewayLoginView sends users whose
    # profile has completed_onboarding=False here); defaults to a legacy
    # mitxonline.odl.local:8013 host, which doesn't resolve in this stack.
    "MITXONLINE_NEW_USER_LOGIN_URL": f"https://mitxonline.{root_domain}/create-profile",
    # Base of most Learn links and redirects mitxonline builds, such as the
    # post-purchase dashboard redirect. The default maps MITX_ONLINE_ENVIRONMENT
    # to a Learn host and falls back to https://learn.mit.edu, so "local" would
    # send them to production Learn.
    "MIT_LEARN_BASE_URL": f"https://learn.{root_domain}",
    # The pair each deployed stack sets. The gateway's cors plugin reads it too
    # (definition.cors_allow_origins), so Learn's pages can't call mitxonline
    # cross-origin without it.
    "CORS_ALLOWED_ORIGINS": f"https://mitxonline.{root_domain},https://learn.{root_domain}",
    # The trailing "&" is deliberate: the view appends "?redirect_url=..." to this
    # value, and it keeps no_redirect parsing as exactly "1".
    # With the openedx app enabled, the deployed envs' https://lms.<root domain>/logout
    # is the right value.
    "LOGOUT_REDIRECT_URL": f"https://mitxonline.{root_domain}/logout?no_redirect=1&",
    "MITX_ONLINE_USE_S3": "False",
    "DEBUG": "False",
    "ALLOWED_HOSTS": "*",
    "USE_X_FORWARDED_PORT": "True",
    # The CA_BUNDLE_CONFIGMAP mount below.
    "REQUESTS_CA_BUNDLE": f"{CA_BUNDLE_DIR}/ca-certificates.crt",
    "SSL_CERT_FILE": f"{CA_BUNDLE_DIR}/ca-certificates.crt",
    # Apple M4 VMs advertise SME; bundled OpenSSL's CPU probe then SIGILLs on import.
    "OPENSSL_armcap": "0",
    # Site config
    "MITX_ONLINE_ENVIRONMENT": "local",
    "MITX_ONLINE_ADMIN_EMAIL": "admin@ol.test",
    "EXPOSE_OIDC_LOGIN": "True",
    # Keycloak
    "KEYCLOAK_BASE_URL": f"https://sso.ol.{root_domain}",
    # Django's own Keycloak client (account actions). Gateway login uses the
    # mit-learn client, and the admin API mitxonline-b2b-client (see
    # supplied_by in secrets.yaml). Deployed stacks set the ID in their vars and
    # no secret.
    "KEYCLOAK_CLIENT_ID": "ol-mitxonline-client",
    "KEYCLOAK_CLIENT_SECRET": "local-dev-mitxonline-secret",  # pragma: allowlist secret
    # Required for Django's CSRF origin check on POSTs behind HTTPS (e.g. the
    # admin login form); defaults to empty, which rejects them with DEBUG=False.
    "CSRF_TRUSTED_ORIGINS": (
        f"https://mitxonline.{root_domain},https://learn.{root_domain}"
    ),
    # Valkey: this app's logical DBs are 6 and 7. Each app needs its own, or
    # Celery and RedBeat pick up another app's tasks; the allocation is listed
    # under Shared Valkey in local-dev/ARCHITECTURE.md.
    "REDIS_URL": "redis://valkey.local-infra.svc.cluster.local:6379/6",
    "CELERY_BROKER_URL": "redis://valkey.local-infra.svc.cluster.local:6379/7",
    "CELERY_RESULT_BACKEND": "redis://valkey.local-infra.svc.cluster.local:6379/7",
    # Email
    "EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
    "EMAIL_HOST": "mailpit.local-infra.svc.cluster.local",
    "EMAIL_PORT": "1025",
    "EMAIL_USE_TLS": "False",
    # Required mitxonline email var (stub: EMAIL_BACKEND above sends to mailpit,
    # never Mailgun)
    "MAILGUN_SENDER_DOMAIN": "local.dev",
    # Open edX hostnames: the hosts lehrer's LMS and Studio are served on
    # (local-dev/apps/openedx/configmaps/lms-config.yaml). Each has a devstack
    # default in mitxonline's main/settings.py that resolves to nothing here, so
    # unset they produce dead links and failed server-side calls.
    # OPENEDX_COURSE_BASE_URL does not derive from OPENEDX_API_BASE_URL and only
    # its scheme and host are used (courses/utils.py get_courseware_url joins a
    # root-relative template onto it).
    "OPENEDX_API_BASE_URL": f"https://lms.{root_domain}",
    "OPENEDX_COURSE_BASE_URL": f"https://lms.{root_domain}",
    "OPENEDX_STUDIO_API_BASE_URL": f"https://studio.{root_domain}",
    # OPENEDX_BASE_REDIRECT_URL is deliberately absent: its default is
    # OPENEDX_API_BASE_URL, so it follows the value above.
    # The social-auth backend the LMS registers for mitxonline, from
    # ol-social-auth's OLOAuth2 (name = "ol-oauth2"). Both default to
    # mitxpro-oauth2 upstream, the wrong product, and the default is not inert:
    # openedx/migrations/0001_add_oauth_application interpolates
    # OPENEDX_OAUTH_PROVIDER into the edx-oauth-app redirect URI, so leaving it
    # alone registers a callback the LMS never calls. QA and Production both set
    # ol-oauth2 (applications/mitxonline/Pulumi.{QA,Production}.yaml).
    "OPENEDX_OAUTH_PROVIDER": "ol-oauth2",
    "OPENEDX_SOCIAL_LOGIN_PATH": "/auth/login/ol-oauth2/?auth_entry=login",
    # LMS account the service-worker token belongs to. Only the course-run sync
    # paths read it (courses/api.py, import_courserun); the enrollment push
    # authenticates by OPENEDX_SERVICE_WORKER_API_TOKEN alone. Left unset it
    # defaults to None and those calls build a request for user "None".
    "OPENEDX_SERVICE_WORKER_USERNAME": "mitxonline_service_worker",
    # The LMS's half of the pair is OL_OAUTH2_CLIENT_{ID,SECRET} in
    # openedx-secrets (lehrer's local-dev/secret-defaults.yaml); these two must
    # stay identical to those. Committed because they are local-dev-only, like
    # the OPENEDX_API_CLIENT_* stubs above. The pre-deploy Job re-asserts the
    # Application row from them whenever it runs (OPENEDX_OAUTH_APP_STEP in
    # inputs.py).
    "OPENEDX_OAUTH_APP_CLIENT_ID": (
        "edx-oauth-app-local-dev"  # pragma: allowlist secret
    ),
    "OPENEDX_OAUTH_APP_CLIENT_SECRET": (
        "edx-oauth-app-local-dev-secret"  # pragma: allowlist secret
    ),
    # Unified Ecommerce: no local-dev app runs it, so nothing to point at.
    "UNIFIED_ECOMMERCE_URL": "",
    # CyberSource sandbox endpoint (paired with the stub creds in
    # local-dev/apps/mitxonline/secrets.yaml; see the comment there for why these
    # are needed at all).
    "MITOL_PAYMENT_GATEWAY_CYBERSOURCE_SECURE_ACCEPTANCE_URL": (
        "https://testsecureacceptance.cybersource.com/pay"
    ),
}
# The developer's gitignored stack file, Pulumi.Dev.yaml next to this program
# (pulumi-app-apply.sh creates it on the first apply; the .example lists the
# keys). `vars` (env) and `web_memory_limit` are the deployed stacks' keys; the
# other three memory limits are local keys in the same style.
stack_config = pulumi.Config("mitxonline")
env_vars, override_keys = merged_env(
    LOCAL_ENV_DEFAULTS, stack_config.get_object("vars"), root_domain
)
if override_keys:
    # Key names only, never values: the file may hold secrets.
    sys.stderr.write(f"mitxonline:vars overrides: {', '.join(override_keys)}\n")
# The YAML-applied apps take overrides from configmaps/app-env.local.yaml; for
# this app that file is inert, so name its keys instead of letting it sit
# unread (names only: the values may be secrets). Delete this check once no
# app reads app-env.local.yaml.
leftover = APP_DIR / "configmaps/app-env.local.yaml"
if leftover.exists():
    keys = ", ".join((yaml.safe_load(leftover.read_text()) or {}).get("data") or {})
    sys.stderr.write(
        f"configmaps/app-env.local.yaml is not read: move {keys or 'its data:'} "
        "under mitxonline:vars in pulumi/Pulumi.Dev.yaml, then delete it\n"
    )
# Which image runs is the one input a teammate cannot see from the files.
sys.stderr.write(f"image: {repository}:{tag}\n")

# An explicit kubeconfig path, as the core and apps-infra stacks pass. Tilt hands
# the commands it runs a KUBECONFIG under ~/Library/Application Support/tilt-dev
# (the macOS path) that exists only while Tilt runs, and the provider records the
# kubeconfig it was given as a provider input: left to the environment, `tilt
# down` after Tilt has exited (its delete command runs this program's destroy)
# and teardown.sh would read a file that is gone and fail to reach the cluster.
kubeconfig = str(
    Path(pulumi.Config().get("kubeconfig") or "~/.kube/config").expanduser()
)
provider = k8s.Provider("k3d-local", context="local-dev", kubeconfig=kubeconfig)
opts = ResourceOptions(providers=[provider])

# Deployed stacks get these variables from Vault through VSO. Here one plain
# Secret carries the same names, with the values secrets.yaml gives them.
local_secrets = manifest(APP_DIR / "secrets.yaml", root_domain)
# The admin API's client pair, which apps-infra writes into this namespace (the
# deployed stacks read it from keycloak-admin-b2b in Vault).
MOUNTED_SECRETS = [SECRET_NAME, "mitxonline-keycloak-admin"]
unmounted = sorted(set(local_secrets["supplied_by"].values()) - set(MOUNTED_SECRETS))
if unmounted:
    msg = f"secrets.yaml supplied_by names Secrets no workload mounts: {unmounted}"
    raise ValueError(msg)
secret_data = local_secret_data(
    secret_env_names(),
    local_secrets["values"],
    local_secrets["unset"],
    supplied_elsewhere=local_secrets["supplied_by"],
)
secret = k8s.core.v1.Secret(
    SECRET_NAME,
    metadata=k8s.meta.v1.ObjectMetaArgs(
        name=SECRET_NAME, namespace=definition.NAMESPACE, labels=LABELS
    ),
    string_data=secret_data,
    opts=opts,
)


# Laptop-sized; the definition's defaults are sized for production nodes. The
# memory limit of each workload is stack config under the deployed stacks' key
# for the webapp (`mitxonline:web_memory_limit`) and three more in that style;
# the requests stay as written here.
def _workload(cpu: str, memory: str, limit_key: str, limit: str):
    return definition.WorkloadResources(
        requests={"cpu": cpu, "memory": memory},
        limits={"memory": stack_config.get(limit_key) or limit},
    )


sizing = definition.MitxonlineSizing(
    web=_workload("100m", "512Mi", "web_memory_limit", "1Gi"),
    pre_deploy=_workload("100m", "512Mi", "pre_deploy_memory_limit", "2Gi"),
    celery_beat=_workload("10m", "384Mi", "beat_memory_limit", "384Mi"),
    # One merged worker locally; the definition sizes it from the largest queue.
    celery_queues=dict.fromkeys(
        definition.CELERY_QUEUE_NAMES,
        _workload("50m", "256Mi", "worker_memory_limit", "1Gi"),
    ),
)

bindings = definition.MitxonlineBindings(
    env_suffix=ENV_SUFFIX,
    hostnames=definition.MitxonlineHostnames(
        api=f"api.mitxonline.{root_domain}",
        frontend=f"mitxonline.{root_domain}",
        learn_api=f"api.learn.{root_domain}",
    ),
    environment_variables=env_vars,
    k8s_labels=LABELS,
    secret_names=MOUNTED_SECRETS,
    # The shared mit-learn client, as deployed (sso/mitlearn); apps-infra writes
    # this Secret into the mitxonline namespace.
    oidc_secret_name="ol-mitlearn-oidc",  # noqa: S106  # pragma: allowlist secret
    application_docker_tag=tag,
    cluster=definition.ClusterCapabilities(
        registry="direct", autoscalers=False, pod_monitors=False
    ),
    image_repository=repository,
    min_replicas=1,
    sizing=sizing,
    granian_reload=tilt_built,
    celery_topology="merged",
    # Local edX runs on lms.<root>, outside .learn.<root>, so it never reaches
    # the gate. Test it with a stand-in LMS (see PR #6402).
    openedx_login_gate=definition.OpenedxLoginGate(
        lms_host=f"courses.learn.{root_domain}"
    ),
)

secrets_hash = hashlib.sha256(
    json.dumps(secret_data, sort_keys=True).encode()
).hexdigest()
deployed_config = definition.application_config(bindings)
app_config = deployed_config.model_copy(
    update={
        # Local-dev's fresh-database, Open edX and ol-analytics-api steps; see
        # CONFIGURE_WAGTAIL_STEP, OPENEDX_OAUTH_APP_STEP and
        # ANALYTICS_API_OAUTH_APP_SCRIPT.
        "pre_deploy_commands": with_local_steps(
            deployed_config.pre_deploy_commands,
            # Set by the Tiltfile; see its apply_env.
            analytics_api=os.environ.get("LOCAL_DEV_ANALYTICS_API_ENABLED") == "true",
        ),
        "extra_volumes": [
            k8s.core.v1.VolumeArgs(
                name=CA_BUNDLE_CONFIGMAP,
                config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(
                    name=CA_BUNDLE_CONFIGMAP
                ),
            )
        ],
        "extra_volume_mounts": [
            k8s.core.v1.VolumeMountArgs(
                name=CA_BUNDLE_CONFIGMAP, mount_path=CA_BUNDLE_DIR, read_only=True
            )
        ],
        # Env is in env:, so an env edit changes the pod template; a secrets.yaml
        # edit changes only the Secret, and pods must roll for that too.
        "config_hash_inputs": {"secrets.yaml": secrets_hash},
    }
)

OLApplicationK8s(
    ol_app_k8s_config=app_config,
    opts=ResourceOptions(
        providers=[provider],
        depends_on=[secret],
        # The same hash on the Job's pod template: a secrets.yaml edit
        # (DATABASE_URL, say) then replaces the Job and re-runs migrate and the
        # local steps, which the Deployments' pod-template hash alone would not.
        transforms=[
            partial(
                await_policy, job_annotations={"ol.mit.edu/config-hash": secrets_hash}
            )
        ],
    ),
)

# The gateway routes, built as the deployed __main__.py builds them.
direct_oidc = OLApisixOIDCResources(
    f"ol-mitxonline-k8s-olapisixoidcresources-no-prefix-{ENV_SUFFIX}",
    oidc_config=definition.direct_oidc_config(bindings),
    opts=opts,
)
prefixed_oidc = OLApisixOIDCResources(
    f"ol-mitxonline-k8s-olapisixoidcresources-{ENV_SUFFIX}",
    oidc_config=definition.prefixed_oidc_config(bindings),
    opts=opts,
)
shared_plugins = OLApisixSharedPlugins(
    name="ol-mitxonline-external-service-apisix-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="mitxonline",
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
    name=f"mitxonline-apisix-route-direct-{ENV_SUFFIX}",
    k8s_namespace=definition.NAMESPACE,
    k8s_labels=LABELS,
    route_configs=definition.direct_route_configs(
        bindings,
        oidc=direct_oidc,
        shared_plugin_config_name=shared_plugins.resource_name,
    ),
    opts=ResourceOptions(providers=[provider], delete_before_replace=True),
)
OLApisixRoute(
    name=f"mitxonline-apisix-route-prefixed-{ENV_SUFFIX}",
    k8s_namespace=definition.NAMESPACE,
    k8s_labels=LABELS,
    route_configs=definition.prefixed_route_configs(
        bindings,
        oidc=prefixed_oidc,
        shared_plugin_config_name=shared_plugins.resource_name,
    ),
    opts=ResourceOptions(providers=[provider], delete_before_replace=True),
)
# The Open edX login gate, as the deployed stack builds it. Without a stand-in
# LMS, its login path returns 503.
gate_upstream = OLApisixExternalUpstream(
    f"mitxonline-openedx-login-gate-upstream-{ENV_SUFFIX}",
    external_upstream_config=definition.openedx_login_gate_upstream_config(
        bindings.openedx_login_gate, ENV_SUFFIX, LABELS
    ),
    opts=opts,
)
OLApisixRoute(
    name=f"mitxonline-openedx-login-gate-route-{ENV_SUFFIX}",
    k8s_namespace=definition.NAMESPACE,
    k8s_labels=LABELS,
    route_configs=definition.openedx_login_gate_route_configs(
        bindings,
        oidc=prefixed_oidc,
        shared_plugin_config_name=shared_plugins.resource_name,
        upstream_name=gate_upstream.resource_name,
    ),
    opts=ResourceOptions(
        providers=[provider], delete_before_replace=True, depends_on=[gate_upstream]
    ),
)
