"""Provision the JupyterHub Data deployment.

Key differences from the existing jupyterhub/deployment.py:
- No OLApisixOIDCResources — JupyterHub owns auth via GenericOAuthenticator
- APISIX route provides TLS termination and WebSocket proxying only
- JUPYTERHUB_CRYPT_KEY is required because GenericOAuthenticator runs with
  enable_auth_state, which encrypts stored access/refresh tokens
- Notebooks authenticate to Starburst Galaxy themselves via Galaxy's OAuth2
  flow; the hub does not hand them a token (see the comment on singleuser
  extraEnv below)
- The singleuser postStart hook seeds every notebook template baked into the
  image into each user's persistent home directory, without clobbering edits
- Uses EFS dynamic storage (efs-sc) for per-user home directories
- Mounts one RWX EFS volume at ~/shared_nb in every user pod so notebooks can
  be shared by link (see _SHARED_NOTEBOOKS_MOUNT_PATH)
- No course image pre-puller
"""

import base64
import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference

from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.lib.versions import JUPYTERHUB_CHART_VERSION, MARIMO_JUPYTERLAB_VERSION
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole
from ol_infrastructure.components.services.apisix import (
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
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
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.jupyterhub_config import get_authenticator_config
from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.vault import postgres_role_statements

# Team notebook sharing. Every user pod mounts the same RWX EFS volume here, so
# a notebook saved under it can be shared as
#   https://<domain>/hub/user-redirect/marimo/?file=shared_nb/<user>/<nb>.py
# user-redirect sends each viewer to their OWN server, so the notebook runs as
# the viewer: their Galaxy OAuth login, their profile, and their per-user
# JupyterHub auth state, never the author's. marimo resolves `file` against
# the Jupyter server's working directory (/home/jovyan), so the mount has to
# live under it.
#
# Because a shared link runs code as whoever opens it, only the owner may
# change what is in their folder. The efs-sc access point gives every client
# the same POSIX identity, so permissions can't enforce that. The mounts do
# instead: the whole volume is read-only, and the user's own subdirectory is
# mounted again read-write on top of it. <user> is KubeSpawner's safe slug of
# the username, which is the username itself when it's a valid DNS label.
_SHARED_NOTEBOOKS_MOUNT_PATH = "/home/jovyan/shared_nb"

# PVC names for homes: KubeSpawner's default for the efs-sc homes, and a
# distinct one for the efs-jovyan-sc homes that replace them.
# bin/migrate-jupyterhub-data-homes maps claim-<x> to home-<x>.
EFS_SC_HOME_PVC_NAME_TEMPLATE = "claim-{user_server}"
JOVYAN_HOME_PVC_NAME_TEMPLATE = "home-{user_server}"
# See the home_owned_by_jovyan flag in provision_jupyterhub_data_deployment.
_HOME_PVC_FROM_TEMPLATE_CONFIG = "\nc.KubeSpawner.remember_pvc_name = False\n"

# uv's cache, which also holds every notebook's sandbox environment. The image
# builds the shipped templates' environments into a seed directory
# (UV_CACHE_SEED_DIR, set by the image), and an init container copies that into
# the node-local volume mounted here. The path must match the one the image
# built with: uv environments hold their own absolute path.
# The marimo_data stack deploys this service under the same name.
NOTEBOOK_PUBLISHER_SERVICE = "notebook-publisher"
NOTEBOOK_PUBLISHER_ACCESS_SCOPE = (
    f"access:services!service={NOTEBOOK_PUBLISHER_SERVICE}"
)

_UV_CACHE_DIR = "/var/cache/uv"
_UV_CACHE_VOLUME = "uv-cache"
# The seed is about 1Gi. Each further notebook environment hardlinks into the
# wheel cache, so it adds only the packages that notebook is first to use. The
# kubelet evicts the pod if the volume grows past this.
_UV_CACHE_SIZE_LIMIT = "10Gi"
# One cp call, so hardlinks between the wheel cache and the environments
# survive (without them the copy is 2.4x the size and uv recompiles every
# module). It copies the seed's entries rather than the directory itself:
# `cp -a seed/. cache/` also tries to set the times of the mount point, which
# the notebook user doesn't own, and exits non-zero.
#
# KubeSpawner runs every init container string through str.format to expand
# {username} and friends, so the command has no ${VAR} braces and find's
# placeholder is written {{}}.
_UV_CACHE_SEED_COMMAND = (
    'find "$UV_CACHE_SEED_DIR" -mindepth 1 -maxdepth 1 '
    '-exec cp -a -t "$UV_CACHE_DIR" {{}} +'
)
_MARIMO_AI_DEFAULTS_SCRIPT = "/etc/marimo/marimo_ai_defaults.py"
_MARIMO_AI_DEFAULTS_JSON = "/etc/marimo/ai_defaults.json"
# JupyterLab reads overrides.json from <sys.prefix>/share/jupyter/lab/settings,
# and the image's prefix is /opt/conda (quay.io/jupyter/minimal-notebook).
_LAB_SETTINGS_OVERRIDES_PATH = "/opt/conda/share/jupyter/lab/settings/overrides.json"
# The marimo labextension registers its widget factory as "marimo" with
# "python" among its fileTypes, but it is defaultFor only *_mo.py. Making it
# the default viewer for "python" opens every .py in marimo on double-click.
# "Open With > Editor" still edits a helper module as text.
_LAB_SETTINGS_OVERRIDES = {
    "@jupyterlab/docmanager-extension:plugin": {
        "defaultViewers": {"python": "marimo"},
    },
}
# KubeSpawner profile list: currently defines Standard and Large CPU/memory tiers.
_PROFILE_LIST = f"""
c.KubeSpawner.profile_list = [
    {{
        "display_name": "Standard",
        "description": "2 CPU / 8 GB — general data analysis",
        "default": True,
        "kubespawner_override": {{
            "cpu_guarantee": 0.5,
            "cpu_limit": 2,
            "mem_guarantee": "2G",
            "mem_limit": "8G",
            "image": "ghcr.io/mitodl/marimo-jupyterlab:{MARIMO_JUPYTERLAB_VERSION}",
        }},
    }},
    {{
        "display_name": "Large",
        "description": "4 CPU / 32 GB — heavy computation",
        "kubespawner_override": {{
            "cpu_guarantee": 1,
            "cpu_limit": 4,
            "mem_guarantee": "8G",
            "mem_limit": "32G",
            "image": "ghcr.io/mitodl/marimo-jupyterlab:{MARIMO_JUPYTERLAB_VERSION}",
        }},
    }},
]
"""

# Common JupyterHub settings shared across the hub configuration
_COMMON_HUB_CONFIG = """
import os

c.JupyterHub.db_url = os.environ["DATABASE_URL"]
c.Authenticator.allow_all = True

# Read GenericOAuthenticator credentials from Vault-injected environment variables
c.GenericOAuthenticator.client_id = os.environ.get("OAUTH_CLIENT_ID", "")
c.GenericOAuthenticator.client_secret = os.environ.get("OAUTH_CLIENT_SECRET", "")
"""

# Hands the user's Keycloak name and email to the singleuser postStart hook,
# which makes them the git identity unless the user has set their own. Only
# identity goes through the environment, never a token: the pod's environment
# is fixed for its lifetime.
#
# auth_state is None for a user with no stored state, e.g. one an admin spawns
# before they have logged in. A claim that is absent or null becomes "", which
# the postStart step skips. The value is escaped twice: KubeSpawner runs
# environment values through str.format (braces doubled), and Kubernetes
# expands $(VAR) in an env value and reduces $$ to $ (each $ doubled).
_GIT_IDENTITY_HOOK = """
def set_git_identity_env(spawner, auth_state):
    if auth_state is None:
        return
    userinfo = auth_state["oauth_user"]
    identity = {
        "OL_GIT_USER_NAME": userinfo.get("name") or "",
        "OL_GIT_USER_EMAIL": userinfo.get("email") or "",
    }
    spawner.environment = {
        **spawner.environment,
        **{
            key: value.replace("{", "{{")
            .replace("}", "}}")
            .replace("$", "$$")
            for key, value in identity.items()
        },
    }


c.KubeSpawner.auth_state_hook = set_git_identity_env
"""

# Runs in the singleuser postStart hook. For each of user.name and user.email:
# skip if the hub had no value, keep the user's own setting if their global
# git config (on the persistent home volume, includes followed) already has
# one, otherwise set it. Once seeded, the value counts as the user's own, so a
# later change in Keycloak does not reach git.
_GIT_IDENTITY_SEED_COMMAND = (
    '[ -z "$OL_GIT_USER_NAME" ] || '
    "git config --global --includes user.name >/dev/null || "
    'git config --global user.name "$OL_GIT_USER_NAME"; '
    '[ -z "$OL_GIT_USER_EMAIL" ] || '
    "git config --global --includes user.email >/dev/null || "
    'git config --global user.email "$OL_GIT_USER_EMAIL"; '
)


def provision_jupyterhub_data_deployment(  # noqa: PLR0913
    stack_info: StackInfo,
    domain_name: str,
    trino_host: str,
    namespace: str,
    vault_config: Config,
    db_config: OLPostgresDBConfig,
    jupyterhub_data_db: OLAmazonDB,
    cluster_stack: StackReference,
    service_trust_role: OLEKSTrustRole,
    application_labels: dict[str, str],
    k8s_global_labels: dict[str, str],
    workload_labels: dict[str, dict[str, str]],
    service_account_name: str,
    jupyterhub_data_config: Config,
) -> kubernetes.helm.v3.Release:
    """Provision the JupyterHub data Helm release.

    Authentication is GenericOAuthenticator against Keycloak for the hub itself.
    Query clients authenticate to Starburst Galaxy separately, via Galaxy's own
    OAuth2 flow from inside the notebook — the hub injects no Trino credentials.
    """
    base_name = "jupyterhub-data"
    env_name = stack_info.name
    vault_policy_hcl = (
        Path(__file__).parent.joinpath("jupyterhub_data_policy.hcl").read_text()
    )
    authenticator_class = (
        jupyterhub_data_config.get("authenticator_class") or "generic-oauth"
    )
    keycloak_base_url = jupyterhub_data_config.get("keycloak_base_url") or ""
    keycloak_realm = jupyterhub_data_config.get("keycloak_realm") or "ol-data-platform"
    trino_catalog = (
        jupyterhub_data_config.get("trino_catalog") or "ol_data_lake_production"
    )
    aws_region = Config("aws").require("region")
    # Homes on efs-sc show every file as the access point's uid (50000), not
    # jovyan's 1000, so git refuses every repository in them as "dubious
    # ownership". efs-jovyan-sc homes are owned by jovyan. The shared notebooks
    # volume stays on efs-sc on purpose: git must keep refusing other users'
    # repositories under shared_nb, since trusting one would let its config
    # (e.g. core.fsmonitor) run commands as whoever browses into it.
    #
    # A new PVC name keeps the efs-sc homes intact for rollback. An environment
    # turns this on only after bin/migrate-jupyterhub-data-homes has copied its
    # existing homes, since a user's next server start gets the new PVC.
    #
    # KubeSpawner saves each server's PVC name in its state and reuses it over
    # pvc_name_template (remember_pvc_name, on by default). The hub turns that
    # off, so the template alone picks the PVC and flipping the flag moves
    # users in either direction; otherwise turning it on would leave existing
    # users on claim-<x>, and turning it off again would leave them on home-<x>.
    if jupyterhub_data_config.get_bool("home_owned_by_jovyan"):
        home_storage = {
            "storageClass": cluster_stack.require_output("efs_jovyan_storageclass"),
            "pvcNameTemplate": JOVYAN_HOME_PVC_NAME_TEMPLATE,
        }
    else:
        home_storage = {
            "storageClass": "efs-sc",
            "pvcNameTemplate": EFS_SC_HOME_PVC_NAME_TEMPLATE,
        }
    # A cross-Region inference profile ID (e.g. us.anthropic.claude-sonnet-5);
    # newer models can't be invoked on demand by bare foundation-model ID.
    bedrock_model = f"bedrock/{jupyterhub_data_config.require('bedrock_chat_model')}"
    marimo_ai_defaults = {
        # edit_model falls back to chat_model, so one key covers both.
        "ai": {
            "models": {"chat_model": bedrock_model, "custom_models": [bedrock_model]}
        }
    }

    # Vault Policy
    vault_policy = vault.Policy(
        f"ol-{base_name}-vault-policy-{stack_info.env_suffix}",
        name=base_name,
        policy=vault_policy_hcl,
    )

    # Vault K8S Auth Backend Role — bound only to the jupyterhub-data namespace.
    # This role's policy covers JupyterHub hub secrets (OIDC creds, crypt key, DB
    # creds) and must not be shared with other namespaces. The marimo namespace
    # has its own dedicated Vault policy and auth role in the marimo_data stack.
    # Only VSO's ServiceAccount, which OLVaultK8SResources names
    # f"{application_name}-vault", logs in here. Single-user pods share this
    # namespace, so binding "*" would leave the role open to any SA in it that
    # is ever granted system:auth-delegator.
    vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"ol-{base_name}-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=base_name,
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=[f"{base_name}-vault"],
        bound_service_account_namespaces=[namespace],
        token_policies=[vault_policy.name],
    )

    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name=base_name,
            namespace=namespace,
            labels=k8s_global_labels,
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=vault_k8s_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[vault_k8s_auth_backend_role],
        ),
    )

    # Docker-registry pull secret for ghcr.io/mitodl private images.
    # The odlbot GitHub PAT (read:packages scope) is read from Vault at deploy time
    # and written as a kubernetes.io/dockerconfigjson secret so KubeSpawner can pull
    # ghcr.io/mitodl/marimo-jupyterlab without anonymous auth (which GHCR denies).
    odlbot_github_token = vault.generic.get_secret_output(
        path="secret-operations/global/odlbot-github-access-token",
        opts=InvokeOptions(parent=vault_policy),
    )
    ghcr_pull_secret_name = f"{base_name}-ghcr-pull-secret"

    def _make_dockerconfig_json(token: str) -> str:
        auth = base64.b64encode(f"odlbot:{token}".encode()).decode()
        return json.dumps({"auths": {"ghcr.io": {"auth": auth}}})

    ghcr_pull_secret = kubernetes.core.v1.Secret(
        f"{base_name}-ghcr-pull-secret-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=ghcr_pull_secret_name,
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        type="kubernetes.io/dockerconfigjson",
        string_data={
            ".dockerconfigjson": odlbot_github_token.data["value"].apply(
                _make_dockerconfig_json
            ),
        },
        opts=ResourceOptions(depends_on=[vault_k8s_resources]),
    )

    # VaultStaticSecret: syncs ol-marimo-client OIDC credentials into the namespace
    # so the hub pod can read OAUTH_CLIENT_ID and OAUTH_CLIENT_SECRET from env vars.
    oidc_secret_name = "jupyterhub-data-oidc-secret"  # noqa: S105  # pragma: allowlist secret
    oidc_static_secret = OLVaultK8SSecret(
        f"{base_name}-oidc-static-secret-{stack_info.env_suffix}",
        resource_config=OLVaultK8SStaticSecretConfig(
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=oidc_secret_name,
            exclude_raw=True,
            excludes=[".*"],
            includes=["client_id", "client_secret"],
            labels=k8s_global_labels,
            mount="secret-operations",
            mount_type="kv-v1",
            name=f"{base_name}-oidc-secret",
            namespace=namespace,
            path="sso/marimo",
            refresh_after="1h",
            templates={
                "OAUTH_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                "OAUTH_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',  # pragma: allowlist secret  # noqa: E501
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(depends_on=[vault_k8s_resources]),
    )

    # VaultStaticSecret: syncs the 32-byte hex JUPYTERHUB_CRYPT_KEY used to encrypt
    # auth state (access + refresh tokens) stored in the JupyterHub database.
    crypt_key_secret_name = "jupyterhub-data-crypt-key"  # noqa: S105  # pragma: allowlist secret
    crypt_key_static_secret = OLVaultK8SSecret(
        f"{base_name}-crypt-key-static-secret-{stack_info.env_suffix}",
        resource_config=OLVaultK8SStaticSecretConfig(
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=crypt_key_secret_name,
            exclude_raw=True,
            excludes=[".*"],
            includes=["JUPYTERHUB_CRYPT_KEY"],
            labels=k8s_global_labels,
            mount="secret-operations",
            mount_type="kv-v1",
            name=f"{base_name}-crypt-key",
            namespace=namespace,
            path="jupyterhub-data/crypt-key",
            refresh_after="24h",
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(depends_on=[vault_k8s_resources]),
    )

    # Vault Database Backend and dynamic credentials secret
    vault_backend_config = OLVaultPostgresDatabaseConfig(
        db_name=db_config.db_name or "jupyterhub_data",
        mount_point=f"postgres-{base_name}",
        db_admin_username=db_config.username,
        db_admin_password=db_config.password.get_secret_value(),
        db_host=jupyterhub_data_db.db_instance.address,
        role_statements=postgres_role_statements,
    )

    app_vault_backend = OLVaultDatabaseBackend(
        vault_backend_config,
        opts=ResourceOptions(depends_on=[jupyterhub_data_db]),
    )

    creds_secret_name = f"{base_name}-db-creds"
    app_db_creds_dynamic_secret = OLVaultK8SSecret(
        f"{base_name}-app-db-creds-vaultdynamicsecret",
        resource_config=OLVaultK8SDynamicSecretConfig(
            name=f"{base_name}-app-db-creds",
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=creds_secret_name,
            exclude_raw=True,
            labels=k8s_global_labels,
            mount=app_vault_backend.db_mount.path,
            namespace=namespace,
            path="creds/app",
            templates={
                "DATABASE_URL": (
                    f'postgresql://{{{{ get .Secrets "username" }}}}'
                    f':{{{{ get .Secrets "password" }}}}'
                    f"@{db_config.instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:"
                    f"{DEFAULT_POSTGRES_PORT}/{db_config.db_name}"
                )
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(depends_on=app_vault_backend),
    )

    # APISIX shared plugins (cors, redirect, response-rewrite, prometheus,
    # opentelemetry, gzip).  OLApisixHTTPRoute merges this list into each
    # referencing route's own plugins, so request-id (added by
    # OLApisixHTTPRouteConfig's validator) no longer has to be restated here.
    shared_plugins = OLApisixSharedPlugins(
        name=f"ol-{base_name}-external-service-apisix-plugins",
        plugin_config=OLApisixSharedPluginsConfig(
            application_name=base_name,
            resource_suffix="ol-shared-plugins",
            render_only=True,
            k8s_namespace=namespace,
            k8s_labels=application_labels,
            # JupyterHub serves its own UI, its API and every single-user server
            # from this one host, so all of its browser traffic is same-origin.
            # An empty list attaches no cors plugin at all.
            cors_allow_origins=[],
            enable_defaults=True,
        ),
    )

    # TLS certificate for the JupyterHub domain. ApisixTls is still required
    # for APISIX to perform TLS termination even when routing via HTTPRoute.
    api_tls_secret_name = f"api-{base_name}-tls-pair"
    OLCertManagerCert(
        f"ol-{base_name}-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name=base_name,
            k8s_namespace=namespace,
            k8s_labels=application_labels,
            create_apisixtls_resource=True,
            dest_secret_name=api_tls_secret_name,
            dns_names=[domain_name],
        ),
    )

    # Gateway API HTTPRoute — replaces legacy ApisixRoute CRD.
    # External-DNS reads spec.hostnames and creates the Route53 record automatically
    # (via --source=gateway-httproute) pointing at the APISIX ELB, so no manual
    # eks:apisix_domains entry is needed.
    # Port 80 is passed as a numeric value because proxy-public exposes port 80
    # named "http", and the port-name-to-number mapping in OLApisixHTTPRoute
    # defaults "http" to the nginx default (8071) which is wrong here.
    OLApisixHTTPRoute(
        name=f"ol-{base_name}-k8s-apisix-httproute-{stack_info.env_suffix}",
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name=base_name,
                shared_plugins=shared_plugins,
                hosts=[domain_name],
                paths=["/*"],
                backend_service_name="proxy-public",
                backend_service_port=80,
                # APISIX only upgrades WebSockets when the routed Service port
                # advertises appProtocol: kubernetes.io/ws. z2jh's proxy-public
                # can't set that on its main port, so websocket=True makes the
                # component create a sibling "proxy-public-ws" Service (selecting
                # the CHP proxy pods) with the hint and route through it. Without
                # this, /marimo/ws (and all kernel/terminal sockets) arrive as
                # plain GETs and marimo reports "kernel not found".
                websocket=True,
                websocket_backend_selector={
                    "app": "jupyterhub",
                    "component": "proxy",
                    "release": base_name,
                },
                # proxy-public maps numeric port 80 -> named targetPort "http".
                websocket_backend_target_port="http",
            ),
        ],
        k8s_namespace=namespace,
        k8s_labels=application_labels,
        opts=ResourceOptions(delete_before_replace=True),
    )

    shared_notebooks_pvc = kubernetes.core.v1.PersistentVolumeClaim(
        f"{base_name}-shared-notebooks-pvc-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{base_name}-shared-notebooks",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
            access_modes=["ReadWriteMany"],
            storage_class_name="efs-sc",
            # EFS does not enforce the requested size; the field is required.
            resources=kubernetes.core.v1.VolumeResourceRequirementsArgs(
                requests={"storage": "50Gi"}
            ),
        ),
        # efs-sc reclaims with Delete, so replacing or deleting this claim
        # removes its access point and a new claim gets an empty one. The old
        # notebooks are then unreachable from the hub (the EFS CSI driver only
        # deletes the directory itself when deleteAccessPointRootDir is set).
        opts=ResourceOptions(protect=True),
    )

    # Kubernetes ServiceAccount annotated with the IRSA role ARN so that
    # single-user pods can invoke Bedrock without long-lived credentials.
    kubernetes.core.v1.ServiceAccount(
        f"jupyterhub-data-service-account-{namespace}-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=service_account_name,
            namespace=namespace,
            labels=k8s_global_labels,
            annotations={
                "eks.amazonaws.com/role-arn": service_trust_role.role.arn,
            },
        ),
        automount_service_account_token=False,
    )

    auth_config = get_authenticator_config(
        {
            "authenticator_class": authenticator_class,
            "keycloak_base_url": keycloak_base_url,
            "keycloak_realm": keycloak_realm,
            "login_service": "MIT OL Data Platform",
            "username_claim": "preferred_username",
            "base_url": f"https://{domain_name}",
        }
    )

    return kubernetes.helm.v3.Release(
        f"{base_name}-{env_name}-application-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            chart="jupyterhub",
            cleanup_on_fail=True,
            max_history=10,
            version=JUPYTERHUB_CHART_VERSION,
            namespace=namespace,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://hub.jupyter.org/helm-chart/",
            ),
            name=base_name,
            skip_crds=False,
            values={
                "ingress": {
                    "enabled": False,
                },
                "cull": {
                    "enabled": True,
                    "every": 300,
                    "timeout": 14400,  # 4 hours idle before culling
                    "maxAge": 0,  # no absolute session limit
                    "users": False,
                },
                "proxy": {
                    "labels": workload_labels["proxy"],
                    "service": {
                        "type": "ClusterIP",
                    },
                    "chp": {
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "64Mi"},
                            "limits": {"memory": "64Mi"},
                        },
                        "pdb": {"enabled": True},
                    },
                },
                "scheduling": {
                    "podPriority": {"enabled": True},
                    # Zero replicas by chart default. Labeled so that raising
                    # the count does not bring up untiered pods.
                    "userPlaceholder": {
                        "labels": workload_labels["user_placeholder"],
                    },
                    "userScheduler": {
                        "enabled": True,
                        "labels": workload_labels["user_scheduler"],
                        # This scheduler's informer caches scale with the total
                        # object count of the whole cluster, not with this
                        # namespace. data-production routinely carries several
                        # thousand pods, which OOMKilled the previous 64Mi cap.
                        "resources": {
                            "requests": {"cpu": "100m", "memory": "256Mi"},
                            "limits": {"memory": "1Gi"},
                        },
                    },
                },
                "hub": {
                    "labels": workload_labels["hub"],
                    "db": {"type": "postgres"},
                    "extraEnv": [
                        {
                            "name": "DATABASE_URL",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": creds_secret_name,
                                    "key": "DATABASE_URL",
                                }
                            },
                        },
                        {
                            "name": "JUPYTERHUB_CRYPT_KEY",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": crypt_key_secret_name,
                                    "key": "JUPYTERHUB_CRYPT_KEY",
                                }
                            },
                        },
                        {
                            "name": "OAUTH_CLIENT_ID",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": oidc_secret_name,
                                    "key": "OAUTH_CLIENT_ID",
                                }
                            },
                        },
                        {
                            "name": "OAUTH_CLIENT_SECRET",
                            "valueFrom": {
                                "secretKeyRef": {
                                    "name": oidc_secret_name,
                                    "key": "OAUTH_CLIENT_SECRET",  # pragma: allowlist secret  # noqa: E501
                                }
                            },
                        },
                    ],
                    "extraConfig": {
                        "hubDataConfig.py": (
                            _COMMON_HUB_CONFIG
                            + _PROFILE_LIST
                            + _GIT_IDENTITY_HOOK
                            + _HOME_PVC_FROM_TEMPLATE_CONFIG
                        ),
                    },
                    "config": auth_config,
                    # The notebook publisher (deployed by the marimo_data
                    # stack into this namespace); the hub proxies
                    # /services/notebook-publisher/ to it. It identifies callers
                    # by their hub token and needs no API token of its own.
                    "services": {
                        NOTEBOOK_PUBLISHER_SERVICE: {
                            "url": (
                                f"http://{NOTEBOOK_PUBLISHER_SERVICE}.{namespace}"
                                ".svc.cluster.local:8000"
                            ),
                            "display": False,
                        },
                    },
                    # The publisher only accepts tokens that carry its access
                    # scope. Neither default role grants it, and a server's
                    # token (JUPYTERHUB_API_TOKEN, what the publish CLI sends)
                    # is limited to its owner's scopes as well as its own, so
                    # both roles get it. These replace JupyterHub's default
                    # role definitions; the other scopes are those defaults.
                    "loadRoles": {
                        "user": {
                            "scopes": ["self", NOTEBOOK_PUBLISHER_ACCESS_SCOPE],
                        },
                        "server": {
                            "scopes": [
                                "users:activity!user",
                                "access:servers!server",
                                NOTEBOOK_PUBLISHER_ACCESS_SCOPE,
                            ],
                        },
                    },
                    "resources": {
                        "requests": {"cpu": "200m", "memory": "512Mi"},
                        "limits": {"memory": "512Mi"},
                    },
                    "pdb": {"enabled": True},
                },
                "prePuller": {
                    "continuous": {"enabled": False},
                    "hook": {"enabled": False},
                },
                "singleuser": {
                    "extraLabels": workload_labels["singleuser"],
                    "serviceAccountName": service_account_name,
                    "image": {
                        "name": "ghcr.io/mitodl/marimo-jupyterlab",
                        "tag": MARIMO_JUPYTERLAB_VERSION,
                        "pullPolicy": "IfNotPresent",
                        "pullSecrets": [ghcr_pull_secret_name],
                    },
                    "cmd": ["jupyterhub-singleuser"],
                    # A postStart hook would race the first notebook open, and
                    # uv would then build the environment the seed already has.
                    "initContainers": [
                        {
                            "name": "seed-uv-cache",
                            "image": (
                                "ghcr.io/mitodl/marimo-jupyterlab:"
                                f"{MARIMO_JUPYTERLAB_VERSION}"
                            ),
                            "command": [
                                "sh",
                                "-c",
                                _UV_CACHE_SEED_COMMAND,
                            ],
                            "env": [{"name": "UV_CACHE_DIR", "value": _UV_CACHE_DIR}],
                            "securityContext": {
                                "runAsUser": 1000,
                                "runAsGroup": 100,
                                "allowPrivilegeEscalation": False,
                            },
                            "volumeMounts": [
                                {
                                    "name": _UV_CACHE_VOLUME,
                                    "mountPath": _UV_CACHE_DIR,
                                },
                            ],
                        },
                    ],
                    # The postStart hook below re-seeds any missing template on
                    # every server start, so this file exists even for a user
                    # who deleted it.
                    "defaultUrl": "/lab/tree/notebooks/getting_started.py",
                    "startTimeout": 300,
                    "networkPolicy": {"enabled": False},
                    # Seed the notebook templates into the user's home on first
                    # login: getting_started.py (minimal starting point), demo.py
                    # (full tour) and README.md. Copying the whole directory means
                    # a new template ships with the image rather than needing a
                    # matching change here.
                    #
                    # cp -n is load-bearing: home is a persistent EFS volume, so
                    # it must never overwrite a copy the user has edited. The
                    # corollary is that template FIXES do not reach users who
                    # already have a copy.
                    #
                    # `|| true` guards the container: a postStart hook that exits
                    # non-zero kills it, and the glob fails if the templates
                    # directory is ever empty.
                    #
                    # It then fills in the Bedrock assistant defaults for any key
                    # the user hasn't set (see marimo_ai_defaults.py), and the
                    # git identity (see _GIT_IDENTITY_SEED_COMMAND).
                    "lifecycleHooks": {
                        "postStart": {
                            "exec": {
                                "command": [
                                    "sh",
                                    "-c",
                                    "mkdir -p /home/jovyan/notebooks && "
                                    "cp -n /usr/local/share/marimo/templates/* "
                                    "/home/jovyan/notebooks/ || true; "
                                    f"{_GIT_IDENTITY_SEED_COMMAND}"
                                    f"python3 {_MARIMO_AI_DEFAULTS_SCRIPT} "
                                    f"{_MARIMO_AI_DEFAULTS_JSON} || true",
                                ]
                            }
                        }
                    },
                    # The extension launches the image's own marimo rather than
                    # `uvx marimo[sandbox]`. marimo's AI assistant runs in that
                    # server process and needs pydantic-ai with its Bedrock extra,
                    # which the image installs and an ad hoc uvx environment
                    # doesn't have. --sandbox is still passed, so each notebook
                    # still runs in its own uv environment built from its
                    # `/// script` PEP 723 header. timeout=120 covers building one
                    # on first open.
                    "extraFiles": {
                        "jupyter-server-config": {
                            "mountPath": "/etc/jupyter/jupyter_server_config.py",
                            "stringData": "c.MarimoProxyConfig.timeout = 120\n",
                        },
                        "lab-settings-overrides": {
                            "mountPath": _LAB_SETTINGS_OVERRIDES_PATH,
                            "stringData": json.dumps(_LAB_SETTINGS_OVERRIDES),
                        },
                        "marimo-ai-defaults-script": {
                            "mountPath": _MARIMO_AI_DEFAULTS_SCRIPT,
                            "stringData": Path(__file__)
                            .parent.joinpath("marimo_ai_defaults.py")
                            .read_text(),
                        },
                        "marimo-ai-defaults": {
                            "mountPath": _MARIMO_AI_DEFAULTS_JSON,
                            "stringData": json.dumps(marimo_ai_defaults),
                        },
                    },
                    # Endpoint only, no credential. Starburst Galaxy authenticates
                    # query clients itself: the notebook uses Galaxy's OAuth2
                    # flow (trino.auth.OAuth2Authentication), Galaxy federates
                    # the login to Keycloak SSO, and Galaxy issues its own
                    # token. Galaxy has no issuer trust for a third-party IdP,
                    # so a Keycloak access token passed as a bearer JWT is
                    # rejected with "401 Authentication required". Injecting one
                    # here would also go stale within minutes, since a pod's
                    # environment is fixed for the pod's lifetime.
                    #
                    # TRINO_USER is deliberately absent. marimo's environment
                    # scanner offers a "Quick add" Trino connection only when
                    # TRINO_HOST, TRINO_USER and TRINO_CATALOG are all set, and
                    # the snippet it generates uses BasicAuthentication or no
                    # auth at all — neither of which Galaxy accepts here. Adding
                    # TRINO_USER would surface a one-click connection that 401s.
                    "extraEnv": {
                        "TRINO_HOST": trino_host,
                        "TRINO_PORT": "443",
                        "TRINO_CATALOG": trino_catalog,
                        "JUPYTERHUB_SINGLEUSER_APP": (
                            "jupyter_server.serverapp.ServerApp"
                        ),
                        # Node-local, not the EFS home volume. uv builds each
                        # notebook's environment inside its cache, and on EFS
                        # that took 161s for a template against 37.5s on local
                        # disk (marimo 0.25.0, data-qa, 2026-10-01). The cost is
                        # that environments for a user's own notebooks are
                        # rebuilt after a pod restart.
                        "UV_CACHE_DIR": _UV_CACHE_DIR,
                        # marimo writes a session cache (cell outputs) to a
                        # __marimo__/ directory next to each notebook it opens.
                        # A viewer's copy of someone else's shared_nb folder is
                        # read-only, so opening their notebook failed with
                        # EROFS. With a pycache prefix set, marimo mirrors
                        # __marimo__/ under it instead (marimo _utils/paths.py
                        # notebook_output_dir), so each viewer's cache, outputs
                        # included, stays in their own home rather than
                        # landing in the shared folder.
                        "PYTHONPYCACHEPREFIX": "/home/jovyan/.cache/pycache",
                        # The prefix would also take Python's bytecode, including
                        # about 1.1k files per sandbox kernel whose /tmp venv is
                        # thrown away, piling up on EFS for the pod's lifetime.
                        # Not writing bytecode costs ~2s of imports per server
                        # process start; kernels recompile on their fresh venvs
                        # either way. marimo's session cache is unaffected.
                        "PYTHONDONTWRITEBYTECODE": "1",
                        # Bedrock via IRSA. boto3 in both the marimo assistant and
                        # notebook kernels resolves the region from these, so
                        # neither has to hardcode one.
                        "AWS_REGION": aws_region,
                        "AWS_DEFAULT_REGION": aws_region,
                    },
                    "storage": {
                        "type": "dynamic",
                        "capacity": "5Gi",
                        "dynamic": home_storage,
                        "extraVolumes": [
                            {
                                "name": "shared-notebooks",
                                "persistentVolumeClaim": {
                                    "claimName": shared_notebooks_pvc.metadata.name,
                                },
                            },
                            {
                                "name": _UV_CACHE_VOLUME,
                                "emptyDir": {"sizeLimit": _UV_CACHE_SIZE_LIMIT},
                            },
                        ],
                        # kubelet creates the subPath directory on first mount.
                        # {username} is expanded by KubeSpawner, not Helm.
                        "extraVolumeMounts": [
                            {
                                "name": _UV_CACHE_VOLUME,
                                "mountPath": _UV_CACHE_DIR,
                            },
                            {
                                "name": "shared-notebooks",
                                "mountPath": _SHARED_NOTEBOOKS_MOUNT_PATH,
                                "readOnly": True,
                            },
                            {
                                "name": "shared-notebooks",
                                "mountPath": (
                                    f"{_SHARED_NOTEBOOKS_MOUNT_PATH}/{{username}}"
                                ),
                                "subPath": "{username}",
                            },
                        ],
                    },
                    "cloudMetadata": {"blockWithIptables": False},
                    # Resource defaults; overridden per-user by KubeSpawner profile_list
                    "memory": {"limit": "8G", "guarantee": "2G"},
                    "cpu": {"guarantee": 0.5},
                    # The uv cache volume counts against the pod's ephemeral
                    # storage. Requesting about the seed's size keeps the
                    # scheduler from packing more of these onto a node than its
                    # disk holds. There is no pod-level limit: one would also
                    # cap what notebooks write to /tmp and /opt/conda, which
                    # nothing caps today. The volume's sizeLimit bounds the cache.
                    "extraResource": {
                        "guarantees": {"ephemeral-storage": "2Gi"},
                    },
                },
            },
            skip_await=False,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[
                app_db_creds_dynamic_secret,
                oidc_static_secret,
                crypt_key_static_secret,
                ghcr_pull_secret,
            ],
        ),
    )
