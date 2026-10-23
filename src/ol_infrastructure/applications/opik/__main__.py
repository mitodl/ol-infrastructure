"""Deploy Opik (Comet's open-source LLM observability platform) on the data EKS cluster.

Opik provides tracing, evaluation, and prompt management for LLM applications.
This stack installs the upstream ``opik/opik`` Helm chart into the ``opik``
namespace and wires it to the pre-existing multi-tenant ClickHouse cluster that
is provisioned by the ``ol-application-clickhouse`` stack (database ``opik_db``,
user ``opik``).

Backing services:
- ClickHouse  — EXTERNAL. The bundled ClickHouse, the Altinity operator, and
  ZooKeeper that the chart would otherwise deploy are disabled; the chart's
  backend talks to ``clickhouse.clickhouse.svc.cluster.local`` over HTTPS
  instead, trusting the CA the ClickHouse stack exports.
- MySQL (state DB), Redis, and MinIO (object storage) — kept as the chart's
  bundled, namespace-local instances. These are internal-only (ClusterIP) and
  use the chart's default credentials, which is acceptable for the CI stack.

The ClickHouse ``opik`` user password lives in Vault at
``secret-clickhouse/credentials`` (key ``opik``). It is synced into a Kubernetes
Secret by the Vault Secrets Operator and injected into the backend Deployment via
``envFrom`` so that it reaches BOTH the ``backend-migrations`` init container and
the main backend container (the chart only wires its ``secretRefs`` into the main
container, not the migration init container) and never lands in a ConfigMap.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export

from bridge.lib.versions import OPIK_CHART_VERSION
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixOIDCVaultCredentials,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AlertTier,
    AWSBase,
    BusinessUnit,
    Component,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
    require_stack_output_value,
)
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
##    Setup + Config Retrieval  ##
##################################

setup_vault_provider()

opik_config = Config("opik")
stack_info = parse_stack()

cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
clickhouse_stack = make_stack_reference(projects.CLICKHOUSE, stack_info.name)
setup_k8s_provider(kubeconfig=require_stack_output_value(cluster_stack, "kube_config"))

OPIK_NAMESPACE = "opik"
OPIK_USERS_GROUP = "opik-users"
if stack_info.env_suffix == "production":
    KEYCLOAK_DOMAIN = "sso.ol.mit.edu"
else:
    KEYCLOAK_DOMAIN = f"sso-{stack_info.env_suffix}.ol.mit.edu"
KEYCLOAK_ISSUER = f"https://{KEYCLOAK_DOMAIN}/realms/ol-platform-engineering"

cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(OPIK_NAMESPACE, ns)
)

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.data,
        "Environment": f"data-{stack_info.env_suffix}",
        "Application": "opik",
        "Owner": "platform-engineering",
    }
)

# Typed labels for OL component resources (e.g. the Vault auth binding).
k8s_labels = K8sGlobalLabels(
    ou=BusinessUnit.data,
    service=Services.opik,
    stack=stack_info,
)

# Plain label dict applied to the raw K8s objects we manage directly.
k8s_global_labels = {
    "pulumi_managed": "true",
    "pulumi_stack": stack_info.k8s_name,
    "ol.mit.edu/stack": stack_info.k8s_name,
    "app.kubernetes.io/managed-by": "pulumi",
}

# External ClickHouse connection details (see the ol-application-clickhouse stack).
CLICKHOUSE_HOST = "clickhouse.clickhouse.svc.cluster.local"
CLICKHOUSE_HTTP_PORT = 8123
CLICKHOUSE_HTTPS_PORT = 8443
# The clickhouse stack serves 8443 only once its clickhouse:tls_listeners_enabled
# rollout step is done; until then 8443 refuses connections and Opik stays on
# plaintext 8123.
clickhouse_tls = require_stack_output_value(clickhouse_stack, "tls_listeners_enabled")
clickhouse_port = CLICKHOUSE_HTTPS_PORT if clickhouse_tls else CLICKHOUSE_HTTP_PORT
CLICKHOUSE_DATABASE = "opik_db"
CLICKHOUSE_USER = "opik"

# Liquibase migrations MUST target a single, stable ClickHouse replica rather
# than the load-balanced ``clickhouse`` service. The changelog bookkeeping
# tables (DATABASECHANGELOG / DATABASECHANGELOGLOCK) are node-local — the
# liquibase-clickhouse extension only makes them replicated when a
# ``liquibaseClickhouse.conf`` declares cluster mode, which the opik chart gives
# us no way to mount. Round-robining the JDBC pool across the three replicas
# therefore splits migration state three ways: each node believes a different
# subset of changesets has run, while the ReplicatedMergeTree metadata they all
# write lives in shared Keeper. That is how 000017_change_tables_to_replicated
# ends up replaying its 12-column ``experiments`` DDL against a ZooKeeper path
# that a further-along replica already advanced to 20 columns, failing with
# "Missing columns: 'optimization_id'" (comet-ml/opik#2464). The Altinity
# operator gives every replica a stable headless service, so pin to replica 0.
CLICKHOUSE_MIGRATIONS_HOST = "chi-clickhouse-default-0-0.clickhouse.svc.cluster.local"

# Vault KV-v2 mount + path holding the ClickHouse credentials. The ``opik`` key
# is the password for the ``opik`` ClickHouse user / ``opik_db`` database.
CLICKHOUSE_VAULT_MOUNT = "secret-clickhouse"
CLICKHOUSE_VAULT_PATH = "credentials"

opik_domain = (
    opik_config.get("k8s_domain") or f"opik-{stack_info.env_suffix}.ol.mit.edu"
)

################################################
##  IRSA + Vault Auth Binding (for VSO sync)  ##
################################################
# No AWS access is required (object storage is the chart's bundled MinIO), so
# no IAM policy is attached. The binding's purpose here is to provision the
# Vault Secrets Operator wiring (VaultConnection / VaultAuth / sync service
# account) plus a Vault policy granting read access to the ClickHouse KV mount.
opik_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="opik",
        namespace=OPIK_NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        vault_policy_path=Path(__file__).parent.joinpath("opik_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name="opik-backend",
        vault_sync_service_account_names="opik-vault",
        k8s_labels=k8s_labels,
    )
)

############################################################
# VSO K8s Secret — ClickHouse credentials as env vars
#
# The Vault Secrets Operator renders the templates below into a K8s Secret. Raw
# Vault keys (admin, tensorzero, ...) are excluded by default; only the
# rendered env-var keys are written. Both keys map to the single ``opik`` user
# password so the backend can use it for runtime queries and migrations alike.
############################################################
clickhouse_credentials_secret_name = (
    "opik-clickhouse-credentials"  # pragma: allowlist secret  # noqa: S105
)
clickhouse_credentials_secret = OLVaultK8SSecret(
    f"opik-clickhouse-credentials-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="opik-clickhouse-credentials",
        namespace=OPIK_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=clickhouse_credentials_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=CLICKHOUSE_VAULT_MOUNT,
        mount_type="kv-v2",
        path=CLICKHOUSE_VAULT_PATH,
        templates={
            "ANALYTICS_DB_PASS": '{{- get .Secrets "opik" -}}',
            "ANALYTICS_DB_MIGRATIONS_PASS": '{{- get .Secrets "opik" -}}',
        },
        refresh_after="1h",
        vaultauth=opik_app.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=opik_app.vault_k8s_resources,
    ),
)

############################################################
# ClickHouse CA
#
# The ClickHouse stack signs its server certificate with a CA of its own and
# exports the CA certificate. The chart's ca-cert-injection init container
# copies the image's Java trust store and imports every *.pem from this
# ConfigMap into the copy, which then replaces the trust store in both the
# backend-migrations init container and the backend. The public CAs stay in
# it, so LLM provider calls are unaffected.
############################################################
CLICKHOUSE_CA_CONFIGMAP_NAME = "clickhouse-ca"
clickhouse_ca_configmap = kubernetes.core.v1.ConfigMap(
    f"opik-clickhouse-ca-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=CLICKHOUSE_CA_CONFIGMAP_NAME,
        namespace=OPIK_NAMESPACE,
        labels=k8s_global_labels,
    ),
    data={"clickhouse-ca.pem": clickhouse_stack.require_output("ca_certificate_pem")},
)

############################################################
# Backend version, per ReplicaSet
#
# The backend image starts with `java -jar opik-backend-$OPIK_VERSION.jar`, in
# both entrypoint.sh and the backend-migrations init container's
# run_db_migrations.sh. The image bakes in the right OPIK_VERSION, but the
# chart also writes OPIK_VERSION into the opik-backend ConfigMap, and that one
# ConfigMap is shared by every ReplicaSet. A chart bump rewrites it in place
# while the old ReplicaSet's pods are still running, and envFrom is resolved
# at each container start, so any old pod that restarts or is recreated (a
# liveness kill, a node drain) launches the old image looking for the new
# jar. That is how data-qa's 2.2.96 pods crash-looped on "Unable to access
# jarfile opik-backend-2.2.103.jar" for days while the 2.2.103 rollout was
# blocked on its migrations.
#
# A ConfigMap named for the version, listed after the chart's in envFrom,
# gives each pod template its own OPIK_VERSION that a later bump cannot
# change. The Pulumi resource name carries the version too, so a bump adds a
# new resource and drops the old one from the program. Pulumi deletes a
# dropped resource only at the end of an update that succeeded, so while a
# Helm upgrade is failing on stuck migrations the old ConfigMap stays, and
# reverting to that version re-registers it unchanged rather than trying to
# create a ConfigMap that still exists. The image tag is pinned to the
# same value: the opik chart publishes appVersion equal to its version, so
# this renders the image the chart would pick anyway, but now from the
# constant the ConfigMap uses.
############################################################
OPIK_BACKEND_VERSION = OPIK_CHART_VERSION
opik_backend_version_configmap = kubernetes.core.v1.ConfigMap(
    f"opik-backend-version-{OPIK_BACKEND_VERSION}-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"opik-backend-version-{OPIK_BACKEND_VERSION}",
        namespace=OPIK_NAMESPACE,
        labels=k8s_global_labels,
    ),
    data={"OPIK_VERSION": OPIK_BACKEND_VERSION},
)

###################################
#   Opik Helm Release (K8S)       #
###################################
opik_helm_release = kubernetes.helm.v3.Release(
    f"opik-{stack_info.env_suffix}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name="opik",
        chart="opik",
        version=OPIK_CHART_VERSION,
        namespace=OPIK_NAMESPACE,
        cleanup_on_fail=True,
        max_history=10,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://comet-ml.github.io/opik/",
        ),
        values={
            # Use the shared data-cluster ClickHouse rather than the bundled one.
            "clickhouse": {"enabled": False},
            "altinity-clickhouse-operator": {"enabled": False},
            "zookeeper": {"enabled": False},
            # Fresh install — there is nothing to migrate from the legacy
            # Bitnami-based subcharts.
            "chartMigration": {"enabled": False},
            "caCerts": {"existingAdditionalCACertsRef": CLICKHOUSE_CA_CONFIGMAP_NAME},
            # Polls system.parts (the opik user is granted SELECT on it in
            # ol-application-clickhouse) every 5m for per-partition part counts
            # and lightweight-delete masks on traces/spans; a distributed lock
            # keeps it to one backend instance.
            #
            # clickhouse.sharding.enabled (which would also turn on the
            # ANALYTICS_DB_CLUSTER_HEALTH_CHECK_ENABLED readiness check) stays
            # off: opik's config.yml says to leave that check false for
            # single-shard, non-Distributed deployments, and it is `critical`,
            # so enabling it here would pull every backend out of rotation.
            "partitionMetrics": {"enabled": True},
            # Opik is internal LLM tracing, so everything is notify. commonLabels
            # reaches the backend, python-backend and frontend Deployments and
            # their pods, which do not share a role, so it carries no component.
            "commonLabels": k8s_labels.workload_labels(alert_tier=AlertTier.notify),
            "mysql": {
                "primary": {
                    "podLabels": k8s_labels.workload_labels(
                        alert_tier=AlertTier.notify, component=Component.database
                    ),
                },
            },
            "minio": {
                "podLabels": k8s_labels.workload_labels(alert_tier=AlertTier.notify),
            },
            # No redis.podLabels: that subchart copies it into the StatefulSet's
            # volumeClaimTemplates, which are immutable.
            "component": {
                "backend": {
                    # Two replicas so a rollout, a node loss, or a single wedged
                    # pod is not a total API outage. Note this does NOT protect
                    # against the ClickHouse connection-pool leak described on the
                    # liveness probe below: both replicas run the same periodic
                    # jobs against the same ClickHouse and drain their pools at
                    # the same rate, so that failure is correlated, not
                    # independent.
                    "replicaCount": 2,
                    "image": {"tag": OPIK_BACKEND_VERSION},
                    "podDisruptionBudget": {"enabled": True, "minAvailable": 1},
                    # The chart ships only an ephemeral-storage request, which
                    # leaves the pod BestEffort with no cgroup memory limit. That
                    # matters because JAVA_OPTS carries -XX:MaxRAMPercentage=80:
                    # with no limit the JVM resolves that against the *node*, and
                    # on a 92.8Gi data-production node it sized MaxHeapSize at
                    # 74.3Gi while actually using ~1.3Gi. A memory limit fixes
                    # both ends at once -- the pod leaves BestEffort (so it is no
                    # longer first to be evicted under node pressure) and the JVM
                    # sizes the heap from the cgroup instead: 80% of the 6Gi limit
                    # is a 4.8Gi ceiling, leaving ~1.2Gi for metaspace, thread
                    # stacks and direct buffers. Generous against the ~1.3Gi the
                    # pod actually uses; the limit is a ceiling, not a target.
                    "resources": {
                        "requests": {
                            "cpu": "500m",
                            "memory": "2Gi",
                            "ephemeral-storage": "10Gi",
                        },
                        "limits": {"memory": "6Gi"},
                    },
                    # Liveness deliberately probes `type=ready`, not the chart's
                    # `type=alive`. The ClickHouse checks are all `type=ready`, and
                    # the backend's client-v2 connection pool can leak every one of
                    # its 10 slots (opik never calls setMaxConnections /
                    # setConnectionTTL, so connection_ttl=-1 and nothing evicts a
                    # leaked lease). A pod in that state is unrecoverable without a
                    # restart -- config.yml says so outright -- yet stays Running
                    # forever at 0 restarts, which is how opik-backend sat out of
                    # rotation for ~12h on 2026-09-23.
                    #
                    # Upstream leaves ClickHouse out of the liveness gate to avoid
                    # restart-storming the fleet on a transient blip. Two things
                    # bound that here: failureThreshold 30 x periodSeconds 10 means
                    # only ~5 minutes of *sustained* unreadiness restarts anything,
                    # and a liveness failure kills the container, not the pod, so it
                    # is subject to the restart policy's exponential backoff
                    # (CrashLoopBackOff) rather than hot-looping, and the
                    # waitForClickhouse init container does not re-run -- init
                    # containers execute once per pod sandbox, not per container
                    # restart. initialDelaySeconds 120 is far above the measured
                    # 21s container-start-to-Ready (migrations run in an init
                    # container, so they are already done by then).
                    "livenessProbe": {
                        "httpGet": {
                            "path": "/health-check?name=all&type=ready",
                            "port": 8080,
                            "httpHeaders": [
                                {"name": "Accept", "value": "application/json"}
                            ],
                        },
                        "initialDelaySeconds": 120,
                        "periodSeconds": 10,
                        "failureThreshold": 30,
                    },
                    # Gate backend startup on the external ClickHouse being
                    # reachable. The chart's check is curl -k, so it does not
                    # need the CA.
                    "waitForClickhouse": {
                        "clickhouse": {
                            "host": CLICKHOUSE_HOST,
                            "port": clickhouse_port,
                            "protocol": "https" if clickhouse_tls else "http",
                        },
                    },
                    # Non-secret ClickHouse connection settings (these land in
                    # the opik-backend ConfigMap). Passwords are injected via
                    # the secretRef in envFrom below.
                    "env": {
                        # Database-less URL on purpose: opik's Liquibase
                        # migrations hard-code the changelog bookkeeping tables
                        # (DATABASECHANGELOG / DATABASECHANGELOGLOCK, and the
                        # ``...1`` shadow tables created by
                        # 000017_change_tables_to_replicated) into the ``default``
                        # database; only the application tables honor
                        # ANALYTICS_DB_DATABASE_NAME. The connection's default
                        # catalog must therefore stay ``default`` so the live
                        # changelog matches what those changesets manipulate. The
                        # ``opik`` ClickHouse user is granted access to ``default``
                        # for exactly this (see ol-application-clickhouse).
                        #
                        # Migrations run on the legacy ru.yandex.clickhouse
                        # driver (clickhouse-jdbc 0.3.1, via
                        # liquibase-clickhouse), whose URL parser needs
                        # host:port right after // and rejects the newer
                        # jdbc:clickhouse:https:// form. TLS is ssl=true;
                        # sslmode defaults to strict, and with no sslrootcert
                        # it verifies against the JVM default trust store, as
                        # R2DBC and client-v2 do for the runtime connections.
                        # That store is where the CA lands (see caCerts above).
                        "ANALYTICS_DB_MIGRATIONS_URL": (
                            f"jdbc:clickhouse://{CLICKHOUSE_MIGRATIONS_HOST}:{clickhouse_port}"
                            + ("?ssl=true" if clickhouse_tls else "")
                        ),
                        "ANALYTICS_DB_PROTOCOL": "HTTPS" if clickhouse_tls else "HTTP",
                        "ANALYTICS_DB_HOST": CLICKHOUSE_HOST,
                        "ANALYTICS_DB_PORT": str(clickhouse_port),
                        # The 1s default is a single SELECT 1. A new pooled
                        # connection now also pays a TLS handshake, and this
                        # check gates readiness and, through the liveness
                        # probe above, restarts (comet-ml/opik#5397).
                        "ANALYTICS_DB_HEALTH_CHECK_TIMEOUT": "5s",
                        "ANALYTICS_DB_DATABASE_NAME": CLICKHOUSE_DATABASE,
                        "ANALYTICS_DB_USERNAME": CLICKHOUSE_USER,
                        "ANALYTICS_DB_MIGRATIONS_USER": CLICKHOUSE_USER,
                        # Pinned to opik's own application default rather than
                        # the chart's. This is the SSRF guard on the token URL
                        # of an admin-configured custom LLM provider: "strict"
                        # requires https and refuses loopback, link-local
                        # (including 169.254.169.254), RFC 1918, IPv6
                        # unique-local and multicast destinations, while
                        # "relaxed" is a no-op. The backend defaults to strict
                        # (config.yml destinationGuard), which is what we run
                        # today because chart 2.2.35 sets nothing -- but 2.2.43
                        # starts shipping "relaxed" in component.backend.env, and that
                        # ConfigMap reaches us through the envFrom below. Upstream
                        # relaxes it because a self-hosted auth service may sit on
                        # a private range. Setting it here keeps the chart bump
                        # from silently weakening a guard we already run with; if
                        # a custom provider ever does need an internal token URL,
                        # this is the knob, and flipping it is then a deliberate
                        # decision rather than a side effect of a version bump.
                        "LLM_PROVIDER_TOKEN_AUTH_DESTINATION_GUARD": "strict",
                    },
                    # Layer the VSO-synced credentials Secret on top of the
                    # ConfigMap. Listing the Secret last lets it override the
                    # placeholder password values, and because the migration
                    # init container also consumes ``envFrom`` it gets the real
                    # password too. The version ConfigMap comes after the
                    # chart's so its OPIK_VERSION wins (see above).
                    "envFrom": [
                        {"configMapRef": {"name": "opik-backend"}},
                        {
                            "configMapRef": {
                                "name": opik_backend_version_configmap.metadata.name
                            }
                        },
                        {"secretRef": {"name": clickhouse_credentials_secret_name}},
                    ],
                },
                # The frontend is exposed via the Gateway API resources below
                # rather than the chart's own ingress.
                "frontend": {
                    "ingress": {"enabled": False},
                },
            },
        },
        skip_await=False,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[clickhouse_credentials_secret, clickhouse_ca_configmap],
    ),
)

##############################################################
# APISIX ingress: Keycloak OIDC auth + TLS termination
#
# at the APISIX gateway (data cluster Gateway ``apisix`` in the ``operations``
# namespace) rather than by Opik itself. Three route rules share the same backend
# (``opik-frontend`` proxies ``/api`` to the backend) but apply different auth:
#
#   * ``/api/*`` with a Bearer ``Authorization`` header — bearer-token only. SDK /
#     programmatic clients present a Keycloak access token (client-credentials via
#     the ``ol-opik-client`` service account); APISIX validates the JWT and
#     rejects unauthenticated requests rather than redirecting them.
#   * ``/api/*`` without one — session (cookie) auth, so the browser SPA's API
#     calls work after interactive login.
#   * everything else — interactive OIDC. Unauthenticated browsers are
#     redirected to Keycloak for the authorization-code flow.
# The OIDC client_id/client_secret/discovery URL are synced from Vault
# (``secret-operations/sso/opik``, published by the keycloak substructure) into
# a K8s secret by the Vault Secrets Operator and referenced by the plugin via
# ``secretRef`` (only supported on the legacy ApisixRoute/v2 path).
##############################################################

# TLS certificate + ApisixTls binding for the Opik hostname. cert-manager issues
# the cert; the ApisixTls CRD binds it to the domain in APISIX.
OLCertManagerCert(
    f"opik-{stack_info.env_suffix}-tls",
    cert_config=OLCertManagerCertConfig(
        application_name="opik",
        k8s_namespace=OPIK_NAMESPACE,
        k8s_labels=k8s_global_labels,
        create_apisixtls_resource=True,
        dest_secret_name="opik-tls",  # noqa: S106  # pragma: allowlist secret
        dns_names=[opik_domain],
    ),
    opts=ResourceOptions(depends_on=[opik_helm_release]),
)

# Shared plugins applied to every Opik route (cors, http->https redirect,
# prometheus, opentelemetry, request-id).
opik_shared_plugins = OLApisixSharedPlugins(
    name=f"opik-shared-plugins-{stack_info.env_suffix}",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="opik",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=OPIK_NAMESPACE,
        k8s_labels=k8s_global_labels,
        # Opik's UI and API are served from the one host behind OIDC, and the
        # SDK posts traces server-side where CORS never applies, so no origin
        # other than Opik's own is meant to read these responses.  An empty list
        # attaches no cors plugin at all.
        cors_allow_origins=[],
        enable_defaults=True,
    ),
)

# OIDC secret sync (reuses the application's existing Vault auth binding) and
# openid-connect plugin config generation.
opik_oidc = OLApisixOIDCResources(
    f"opik-oidc-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="opik",
        k8s_labels=k8s_global_labels,
        k8s_namespace=OPIK_NAMESPACE,
        oidc_scope="openid profile email",
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        credentials=OLApisixOIDCVaultCredentials(
            mount="secret-operations",
            mount_type="kv-v1",
            path="sso/opik",
            vaultauth=opik_app.vault_k8s_resources.auth_name,
        ),
    ),
    opts=ResourceOptions(depends_on=opik_app.vault_k8s_resources),
)

# Session (cookie) auth: redirect unauthenticated browsers to Keycloak
# (authorization-code flow). Used for the UI *and* for browser-originated API
# calls — the Opik SPA and its API share an origin, so the SPA authenticates
# the user once at ``/`` and then calls ``/api/*`` from the browser carrying
# only the APISIX session cookie (no bearer token).
opik_session_oidc_plugin = opik_oidc.get_full_oidc_plugin_config(unauth_action="auth")
# OSS Opik makes everyone it sees the admin of workspace "default", so a session
# is admitted only when its ID token names the opik-users Keycloak group (the
# groups mapper on ol-opik-client in substructure/keycloak). The plugin checks
# the ID token it holds after any refresh, so membership changes land within
# one access-token lifetime. A non-member gets a 401; /logout/oidc still works.
opik_session_oidc_plugin["config"]["claim_schema"] = {
    "type": "object",
    "required": ["id_token"],
    "properties": {
        "id_token": {
            "type": "object",
            "required": ["groups"],
            "properties": {
                "groups": {"type": "array", "contains": {"const": OPIK_USERS_GROUP}}
            },
        }
    },
}
# Bearer auth: validate a Keycloak access token from the Authorization header
# and reject (rather than redirect) unauthenticated requests, so SDK / machine
# clients get a clean 401 instead of an HTML login page.
opik_bearer_oidc_plugin = opik_oidc.get_full_oidc_plugin_config(unauth_action="deny")
opik_bearer_oidc_plugin["config"]["bearer_only"] = True
# Verify the JWT signature locally against Keycloak's JWKS. Without this the
# plugin falls back to token introspection, and Keycloak reports active: false
# for client-credentials service-account tokens (transient sessions), so every
# SDK request would 401 even with a freshly-issued valid token.
opik_bearer_oidc_plugin["config"]["use_jwks"] = True
# Every client in the realm signs with the same key, so without an audience
# check a client-credentials token from any of them is admitted. The audience
# mapper on ol-opik-client puts its own client id in aud. The issuer is pinned
# here rather than taken from the discovery document at request time.
opik_bearer_oidc_plugin["config"]["claim_validator"] = {
    "issuer": {"valid_issuers": [KEYCLOAK_ISSUER]},
    "audience": {"match_with_client_id": True},
}
# An ID token for ol-opik-client also has that aud, and a non-member can read
# their own from the id_token_hint on the /logout/oidc redirect. ID tokens have
# typ "ID", and only service-account access tokens carry client_id (from the
# service_account client scope), so users' ID and access tokens both get a 401.
opik_bearer_oidc_plugin["config"]["claim_schema"] = {
    "type": "object",
    "required": ["typ", "client_id"],
    "properties": {"typ": {"const": "Bearer"}, "client_id": {"type": "string"}},
}

# Match ``/api/*`` requests that carry a Bearer ``Authorization`` header so they
# are routed to the bearer-only rule; everything else under ``/api`` falls
# through to the session rule (the browser SPA's cookie-authenticated calls).
# This is a ROUTING condition only — it selects the bearer rule when a Bearer
# ``Authorization`` header is present. It does not (and must not) validate the
# against Keycloak's JWKS. Tokens are short-lived per-client JWTs that rotate
# constantly, so there is no fixed value to match here; ``^Bearer\s`` simply
# limits the bearer route to the Bearer auth scheme.
authorization_header_present_expr = {
    "subject": {"scope": "Header", "name": "Authorization"},
    "op": "RegexMatch",
    "value": r"^Bearer\s",
}

opik_apisix_route = OLApisixRoute(
    f"opik-{stack_info.env_suffix}-apisix-route",
    k8s_namespace=OPIK_NAMESPACE,
    k8s_labels=k8s_global_labels,
    route_configs=[
        # /health -> unauthenticated. The chart's frontend nginx serves this
        # path directly (see configmap-frontend-nginx.yaml's "Dedicated
        # healthcheck endpoint" location) for load balancer / uptime checks,
        # so it must bypass OIDC entirely rather than redirect to Keycloak.
        # Highest priority so it wins over the catch-all UI rule below.
        OLApisixRouteConfig(
            route_name="opik-health",
            priority=30,
            hosts=[opik_domain],
            paths=["/health"],
            backend_service_name="opik-frontend",
            backend_service_port=5173,
            shared_plugin_config_name=opik_shared_plugins.resource_name,
        ),
        # /api/* WITH an Authorization header -> bearer-token validation (SDK /
        # programmatic clients). Highest priority so it wins over the session
        # /api rule when a token is present.
        OLApisixRouteConfig(
            route_name="opik-api-bearer",
            priority=20,
            hosts=[opik_domain],
            paths=["/api/*"],
            exprs=[authorization_header_present_expr],
            backend_service_name="opik-frontend",
            backend_service_port=5173,
            shared_plugin_config_name=opik_shared_plugins.resource_name,
            plugins=[OLApisixPluginConfig(**opik_bearer_oidc_plugin)],
        ),
        # /api/* WITHOUT an Authorization header -> session auth, so the browser
        # SPA's cookie-authenticated API calls succeed after interactive login.
        OLApisixRouteConfig(
            route_name="opik-api-session",
            priority=15,
            hosts=[opik_domain],
            paths=["/api/*"],
            backend_service_name="opik-frontend",
            backend_service_port=5173,
            shared_plugin_config_name=opik_shared_plugins.resource_name,
            plugins=[OLApisixPluginConfig(**opik_session_oidc_plugin)],
        ),
        # Everything else -> interactive OIDC for human browsers.
        OLApisixRouteConfig(
            route_name="opik-ui",
            priority=0,
            hosts=[opik_domain],
            paths=["/*"],
            backend_service_name="opik-frontend",
            backend_service_port=5173,
            shared_plugin_config_name=opik_shared_plugins.resource_name,
            plugins=[OLApisixPluginConfig(**opik_session_oidc_plugin)],
        ),
    ],
    opts=ResourceOptions(
        depends_on=[opik_helm_release, opik_oidc, opik_shared_plugins]
    ),
)

export("opik_namespace", OPIK_NAMESPACE)
export("opik_url", f"https://{opik_domain}")
