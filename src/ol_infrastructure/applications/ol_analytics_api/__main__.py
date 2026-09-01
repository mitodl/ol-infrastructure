"""Pulumi program for the ol-analytics-api service.

ol-analytics-api is a small standalone FastAPI service: a B2B multi-tenant
analytics gateway that reads aggregated data out of StarRocks and serves it to
MIT Learn's org-manager dashboard (the first tenant) under
``/api/v1/analytics/*``.  It is *not* a Django monolith, so this program is
modeled on ``applications/learn_ai/__main__.py`` for its overall shape but takes
its cluster/StarRocks/APISIX wiring from ``applications/dagster/__main__.py`` —
the closest analog because, like dagster/superset/opik, this service runs on the
**data** EKS cluster (where StarRocks lives) and is fronted by APISIX with
Keycloak OIDC.

Key wiring decisions (see also ``k8s/README.md`` in the app repo):

* **Data cluster, not the applications cluster.**  The service talks to
  StarRocks over in-cluster DNS, so every stack reference points at
  ``data.{stack}``.  Vault itself is **not** per-cluster: there is a single
  Vault deployment per environment tier (``vault-qa.odl.mit.edu``,
  ``vault-production.odl.mit.edu``) shared by every EKS cluster, exactly like
  every other app in this repo (``vault:address`` below is the same value
  dagster/mit_learn/etc. use).  What *is* cluster-specific is the Kubernetes
  auth **mount** under that one shared Vault server — Vault's kubernetes auth
  method needs each cluster's own API endpoint/CA/JWT issuer, so every EKS
  cluster gets its own ``auth/k8s-<prefix>`` mount path (``k8s-data`` here,
  read from the data cluster stack's ``vault_auth_endpoint`` output below,
  not hardcoded or assumed to be a separate Vault instance).

* **The app fetches its own StarRocks credentials.**  Unlike superset (which
  has the vault-secrets-operator sync a static-ish StarRocks credential into a
  K8s Secret), ``core/db/vault_credentials.py`` does a direct ``hvac``
  Kubernetes login using the pod's own ServiceAccount token and reads
  ``database-starrocks/creds/app`` at startup.  So there is no DB Secret
  to sync here — instead the Vault policy attached to this service's Kubernetes
  auth role must grant read on that dynamic-credentials path.  The Vault role
  name, k8s auth mount, and StarRocks mount are handed to the app as
  ``OL_ANALYTICS_API_VAULT_{ROLE,K8S_MOUNT,STARROCKS_MOUNT}`` env vars, which
  ``core/config.py`` reads.

* **``OLApplicationK8s`` for the Deployment/Service.**  Using the shared
  component (rather than hand-rolling the manifests the app repo keeps for
  dry-run reference) means the ``/health/{startup,readiness,liveness}/`` probe
  paths and timings that ``core/health.py`` was built to match come straight
  from the component instead of being maintained in a second place.  The
  service runs a single Granian worker on port 8000 with no nginx sidecar, so
  ``import_nginx_config`` is off and the probes are re-pointed at 8000.  This is
  the first data-cluster use of ``OLApplicationK8s``; it creates a
  ``SecurityGroupPolicy`` binding the pods to a dedicated security group (see
  ``ol_analytics_api_application_security_group`` below).  It is also the first
  caller of the component's ``container_security_context``: this service is a
  single non-Django container off an image that already runs as an
  unprivileged user, so it can take a read-only root filesystem and a dropped
  capability set that the Django apps sharing the component cannot yet.

* **APISIX + Keycloak OIDC, two separate clients.**  ``OLApisixRoute`` +
  ``OLApisixOIDCResources`` put the openid-connect plugin in front of the
  service so APISIX validates the Keycloak session and forwards the decoded
  claims as the ``X-Userinfo`` header that ``core/auth/userinfo.py`` expects.
  The canonical ``domain`` host uses a dedicated ``ol-analytics-api`` client
  with a full redirect-to-Keycloak flow (matching the dagster/opik pattern).
  The Learn-scoped ``learn_domain`` host -- the one the MIT Learn frontend
  actually calls -- uses a second OIDC resource that instead shares
  mit-learn's own Keycloak client/session (``sso/mitlearn``) with
  ``unauth_action="pass"``, so a browser already logged into mit-learn is
  recognized here without a second redirect (a plain fetch() can't follow an
  OIDC login redirect the way a browser navigation can). See the Learn-scoped
  OIDC resource below for the full rationale.
"""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions, export
from pulumi_aws import ec2

from bridge.lib.constants import mit_learn_session_cookie_name
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
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
from ol_infrastructure.components.services.k8s import (
    GranianConfig,
    OLApplicationK8s,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    docker_image_config_kwargs,
    make_stack_reference,
    merge_otel_resource_attributes,
    parse_stack,
)
from ol_infrastructure.lib.vault import setup_vault_provider

# The container listens on this port. No nginx sidecar sits in front, so the
# Service and the health probes target it directly. Keep this aligned with
# granian_config.port below.
APPLICATION_PORT = 8000

APPLICATION_NAME = "ol-analytics-api"
APPLICATION_NAMESPACE = "ol-analytics"

stack_info = parse_stack()
setup_vault_provider(stack_info)

ol_analytics_api_config = Config("ol_analytics_api")
vault_config = Config("vault")

# Stack references -- data cluster, mirroring applications/dagster/__main__.py.
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
cluster_stack = make_stack_reference(projects.EKS, f"data.{stack_info.name}")
# Sentry is a single SaaS org, not per-env infrastructure -- one default
# stack backs every ol_analytics_api env (QA and Production both read the
# same project/DSN; Sentry environments are distinguished by the
# deployment.environment OTel/Sentry tag, not by separate Sentry projects).
sentry_stack = make_stack_reference(projects.SENTRY, "default")

data_vpc = network_stack.require_output("data_vpc")
k8s_pod_subnet_cidrs = data_vpc["k8s_pod_subnet_cidrs"]

environment_name = f"data-{stack_info.env_suffix}"
aws_config = AWSBase(tags={"OU": "data", "Environment": environment_name})

k8s_global_labels = K8sGlobalLabels(
    ou=BusinessUnit.data,
    service=Services.ol_analytics_api,
    stack=stack_info,
).model_dump()

# Kubernetes provider from the data cluster's kubeconfig.
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# The ol-analytics namespace is declared by the data EKS stack (see
# infrastructure/aws/eks/Pulumi.data.*.yaml `eks:namespaces`); fail fast if it
# is missing rather than silently creating resources in a namespace that has
# not been provisioned.
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(APPLICATION_NAMESPACE, ns)
)

# StarRocks exists on the QA and Production data clusters only (setup_starrocks
# skips CI), so this service is deployed to QA and Production only -- there is
# no CI stack. The mount name itself is not environment-specific: QA and
# Production each run their own, entirely separate Vault deployment, so that
# server (not the mount path) is what scopes the environment.
starrocks_vault_mount_path = "database-starrocks"

########################################################################
# IRSA + Vault Kubernetes auth via OLEKSAuthBinding
########################################################################
# This provisions, for the "ol-analytics-api" application:
#   * an IRSA trust role and the "ol-analytics-api" ServiceAccount the app pod
#     runs as (no IAM policy is attached -- the service needs no direct AWS API
#     access; StarRocks is reached with Vault-issued DB creds and MITx Online
#     over HTTPS),
#   * a Vault Kubernetes auth backend role named "ol-analytics-api" bound to
#     both the app ServiceAccount (which logs in directly for StarRocks creds)
#     and the "ol-analytics-api-vault" ServiceAccount that OLVaultK8SResources
#     creates for the vault-secrets-operator, and
#   * the Vault policy (below) attached to that role.
_vault_policy_text = (
    Path(__file__).parent.joinpath("ol_analytics_api_policy.hcl").read_text()
    + f'\npath "{starrocks_vault_mount_path}/creds/app" {{\n'
    '  capabilities = ["read"]\n'
    "}\n" + f'path "{starrocks_vault_mount_path}/creds/app/*" {{\n'
    '  capabilities = ["read"]\n'
    "}\n"
)

ol_analytics_api_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name=APPLICATION_NAME,
        namespace=APPLICATION_NAMESPACE,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=None,
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        # Create the ServiceAccount the app pod runs as.  core/db/vault_credentials.py
        # uses this account's projected token to authenticate to Vault.
        irsa_service_account_name=APPLICATION_NAME,
        create_irsa_service_account=True,
        # Bind the Vault k8s auth role to both the app SA (direct StarRocks
        # login) and the VSO SA (secret sync).  OLVaultK8SResources names the
        # latter "{application_name}-vault".
        vault_sync_service_account_names=[
            APPLICATION_NAME,
            f"{APPLICATION_NAME}-vault",
        ],
        k8s_labels=K8sGlobalLabels(
            ou=BusinessUnit.data,
            service=Services.ol_analytics_api,
            stack=stack_info,
        ),
        vault_policy_text=_vault_policy_text,
    )
)

# The data cluster's Vault kubernetes auth backend config (vault_secrets_operator.py,
# infrastructure/aws/eks) has no token_reviewer_jwt set, so Vault authenticates each
# login by replaying the caller's own JWT against the Kubernetes TokenReview API --
# which requires that caller's ServiceAccount to hold "system:auth-delegator"
# clusterwide. OLVaultK8SResources (components/services/vault.py) only grants that
# binding to the "{application_name}-vault" VSO sync account, since every other app
# in this repo only ever authenticates to Vault through the vault-secrets-operator.
# ol_analytics_api is the exception: core/db/vault_credentials.py logs in directly
# with the app's own IRSA ServiceAccount to fetch dynamic StarRocks credentials, so
# that account needs the same delegator binding or every login 403s with a generic
# "permission denied" -- see the CI CrashLoopBackOff this was written to fix.
ol_analytics_api_app_auth_delegator_binding = kubernetes.rbac.v1.ClusterRoleBinding(
    f"ol-analytics-api-app-vault-cluster-role-binding-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"{APPLICATION_NAME}:cluster-auth",
        labels=k8s_global_labels,
    ),
    role_ref=kubernetes.rbac.v1.RoleRefArgs(
        api_group="rbac.authorization.k8s.io",
        kind="ClusterRole",
        name="system:auth-delegator",
    ),
    subjects=[
        kubernetes.rbac.v1.SubjectArgs(
            kind="ServiceAccount",
            name=APPLICATION_NAME,
            namespace=APPLICATION_NAMESPACE,
        ),
    ],
    opts=ResourceOptions(parent=ol_analytics_api_auth_binding),
)

########################################################################
# Static application secrets -- SENTRY_DSN
########################################################################
# SENTRY_DSN is the only runtime secret this service needs; everything else is
# non-sensitive config or a short-lived Vault-issued StarRocks credential the
# app fetches itself.  It is delivered the same way learn_ai delivers its static
# secrets: a dedicated kv-v2 mount whose contents the vault-secrets-operator
# syncs into a Kubernetes Secret consumed via envFrom.
#
# The DSN comes from the ol_analytics_api_sentry_dsn output of the
# ol-infrastructure-sentry Pulumi stack (infrastructure/sentry/__main__.py),
# which owns the actual Sentry project + client key -- Pulumi manages the real
# value end-to-end, no manual Vault write required.
ol_analytics_api_secrets_mount = vault.Mount(
    f"ol-analytics-api-secrets-mount-{stack_info.env_suffix}",
    path="secret-ol-analytics-api",
    type="kv-v2",
    options={"version": "2"},
    description="Static application secrets for ol-analytics-api (e.g. SENTRY_DSN).",
    opts=ResourceOptions(delete_before_replace=True),
)

ol_analytics_api_static_vault_secrets = vault.generic.Secret(
    f"ol-analytics-api-secrets-{stack_info.env_suffix}",
    path=ol_analytics_api_secrets_mount.path.apply("{}/secrets".format),
    data_json=sentry_stack.require_output("ol_analytics_api_sentry_dsn").apply(
        lambda dsn: json.dumps({"SENTRY_DSN": dsn})
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

static_secrets_name = "ol-analytics-api-static-secrets"  # pragma: allowlist secret
static_secrets = OLVaultK8SSecret(
    name=f"ol-analytics-api-{stack_info.env_suffix}-static-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="ol-analytics-api-static-secrets",
        namespace=APPLICATION_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_name=static_secrets_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-ol-analytics-api",
        mount_type="kv-v2",
        path="secrets",
        includes=["*"],
        excludes=[],
        exclude_raw=True,
        refresh_after="1m",
        vaultauth=ol_analytics_api_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=ol_analytics_api_auth_binding.vault_k8s_resources,
        depends_on=[ol_analytics_api_static_vault_secrets],
    ),
)

# The OAuth2 client-credentials pair the app uses to authenticate its
# org-manager check to MITx Online (mitodl/mitxonline#3807).  Unlike SENTRY_DSN
# above, no Pulumi stack owns these: the OAuth2 Application record is created by
# hand in MITx Online's Django admin, and its credentials are written once to
# secret-operations.
#
# Read by the vault-secrets-operator directly at runtime and templated into the
# env var names the app expects, rather than being pulled into Pulumi with
# get_secret_output.  That keeps the credential out of Pulumi state entirely and
# lets a rotation in Vault propagate on the operator's own refresh -- rewriting
# the Vault entry is enough, with no `pulumi up` in the loop.  Same shape as
# applications/marimo_data's ol-marimo-app-client secret.
#
# Temporary, along with the round-trip it authenticates: once org-manager status
# is visible in the Keycloak token (mitodl/hq#10594), the app stops calling MITx
# Online and this can be deleted along with the Vault entry.
mitxonline_oauth_secret_name = (
    "ol-analytics-api-mitxonline-oauth"  # pragma: allowlist secret  # noqa: S105
)
mitxonline_oauth_secrets = OLVaultK8SSecret(
    name=f"ol-analytics-api-{stack_info.env_suffix}-mitxonline-oauth-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=mitxonline_oauth_secret_name,
        namespace=APPLICATION_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_name=mitxonline_oauth_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="ol-analytics-api/mitxonline-oauth",
        includes=["client_id", "client_secret"],
        excludes=[".*"],
        exclude_raw=True,
        refresh_after="1h",
        templates={
            "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_ID": (
                '{{ get .Secrets "client_id" }}'
            ),
            "OL_ANALYTICS_API_B2B_DASHBOARD_MITXONLINE_CLIENT_SECRET": (  # pragma: allowlist secret  # noqa: E501
                '{{ get .Secrets "client_secret" }}'
            ),
        },
        vaultauth=ol_analytics_api_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=ol_analytics_api_auth_binding.vault_k8s_resources,
    ),
)

########################################################################
# Application environment
########################################################################
# Non-sensitive config comes from the stack's `ol_analytics_api:env_vars`
# object; the Vault-related values are injected here because they depend on
# stack outputs / the env suffix.
env_vars = dict(ol_analytics_api_config.require_object("env_vars") or {})
env_vars["OL_ANALYTICS_API_VAULT_ADDR"] = vault_config.require("address")
# The Vault k8s auth role name == the OLEKSAuthBinding application_name.
env_vars["OL_ANALYTICS_API_VAULT_ROLE"] = APPLICATION_NAME
env_vars["OL_ANALYTICS_API_VAULT_STARROCKS_MOUNT"] = starrocks_vault_mount_path
# Vault itself (OL_ANALYTICS_API_VAULT_ADDR above) is the single shared
# per-env-tier Vault server, same as every other app -- there is no separate
# "data cluster Vault". What differs per cluster is which Kubernetes auth
# *mount* on that shared server the pod logs into, since Vault's kubernetes
# auth method needs each cluster's own API endpoint/CA/JWT issuer configured
# per mount. The data cluster's mount is `k8s-data` (env_prefix based, NOT
# `k8s-data-{env}`); read it from the cluster stack rather than hardcoding.
# This corrects the placeholder in the app repo's k8s/deployment.yaml.
env_vars["OL_ANALYTICS_API_VAULT_K8S_MOUNT"] = cluster_stack.require_output(
    "vault_auth_endpoint"
)

# Append the org's standard k8s labels to OTEL_RESOURCE_ATTRIBUTES so every
# telemetry signal carries organizational metadata.  (GIT_SHA is baked into the
# image and surfaces as service.version via core/config.py; pod identity is not
# injected here, so the stack's OTEL_RESOURCE_ATTRIBUTES base should not contain
# unexpandable ${GIT_SHA}/${HOSTNAME} shell placeholders.)
merge_otel_resource_attributes(env_vars, k8s_global_labels)

########################################################################
# Pod security group
########################################################################
# OLApplicationK8s always creates a SecurityGroupPolicy binding the app pods to
# this security group.  It mirrors the permissive default used by
# applications/learn_ai: ingress from the cluster pod-subnet CIDRs (so APISIX
# and other in-cluster peers can reach the service) and egress everywhere (Vault
# over HTTPS, MITx Online, the in-cluster StarRocks FE on 9030, and the Grafana
# Alloy OTLP receiver).
ol_analytics_api_application_security_group = ec2.SecurityGroup(
    f"ol-analytics-api-application-security-group-{stack_info.env_suffix}",
    name=f"ol-analytics-api-application-security-group-{stack_info.env_suffix}",
    description="Access control for the ol-analytics-api application pods.",
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
    vpc_id=data_vpc["id"],
    tags=aws_config.tags,
)

########################################################################
# Deployment + Service via OLApplicationK8s
########################################################################
# Health probes match the shared component's paths but are re-pointed at the
# app's own port (8000) since there is no nginx sidecar in front of it.
_probe_configs = {
    "startup_probe": kubernetes.core.v1.ProbeArgs(
        http_get=kubernetes.core.v1.HTTPGetActionArgs(
            path="/health/startup/",
            port=APPLICATION_PORT,
        ),
        initial_delay_seconds=10,
        period_seconds=10,
        failure_threshold=12,
        success_threshold=1,
        timeout_seconds=5,
    ),
    "readiness_probe": kubernetes.core.v1.ProbeArgs(
        http_get=kubernetes.core.v1.HTTPGetActionArgs(
            path="/health/readiness/",
            port=APPLICATION_PORT,
        ),
        initial_delay_seconds=15,
        period_seconds=15,
        # Wider than the default 3x/3s: readiness shares the Granian connection
        # budget with user traffic and its failure is correlated across replicas,
        # so a fast eviction empties the EndpointSlice instead of shedding load.
        # See default_probe_configs in components/services/k8s.py.
        failure_threshold=6,
        timeout_seconds=5,
    ),
    # TCP rather than HTTP so a saturated Granian worker pool cannot fail
    # liveness and trigger a restart that removes capacity from an already
    # overloaded service. Readiness stays on HTTP. See default_probe_configs in
    # components/services/k8s.py.
    "liveness_probe": kubernetes.core.v1.ProbeArgs(
        tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(port=APPLICATION_PORT),
        initial_delay_seconds=30,
        period_seconds=30,
        failure_threshold=3,
        timeout_seconds=3,
    ),
}

ol_analytics_api_k8s = OLApplicationK8s(
    ol_app_k8s_config=OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=env_vars,
        application_name=APPLICATION_NAME,
        application_namespace=APPLICATION_NAMESPACE,
        application_lb_service_name=APPLICATION_NAME,
        application_lb_service_port_name="http",
        k8s_global_labels=k8s_global_labels,
        env_from_secret_names=[static_secrets_name, mitxonline_oauth_secret_name],
        application_security_group_id=ol_analytics_api_application_security_group.id,
        application_security_group_name=Output.from_input(APPLICATION_NAME),
        application_service_account_name=APPLICATION_NAME,
        vault_k8s_resource_auth_name=ol_analytics_api_auth_binding.vault_k8s_resources.auth_name,
        application_image_repository="mitodl/ol-analytics-api-app",
        **docker_image_config_kwargs("OL_ANALYTICS_API"),
        application_min_replicas=ol_analytics_api_config.get_int("min_replicas") or 2,
        # Granian settings are declared here via the shared component so server
        # config is visible and diffable in this stack (and picks up the
        # component's metrics/PodMonitor wiring).
        granian_config=GranianConfig(
            # FastAPI is ASGI; Granian's own default interface is RSGI.
            interface="asgi",
            # Not DEFAULT_WSGI_PORT: there is no nginx sidecar here, so the
            # Service and the health probes talk to Granian directly on
            # APPLICATION_PORT (see import_nginx_config=False below).
            port=APPLICATION_PORT,
            # Scale horizontally on replicas rather than workers-per-pod --
            # this pod is CPU-request-small (100m) and the workload is
            # IO-bound on StarRocks, not CPU-bound.
            workers=1,
            runtime_threads=1,
            # blocking_threads is deliberately unset: Granian forces it to 1
            # on the asgi interface and the component rejects an explicit >1.
            #
            # No websockets anywhere in this service's surface.
            no_ws=True,
            # Matches the app's own LOG_LEVEL=INFO, so Granian's lifecycle
            # lines land in Loki alongside the app's structured output rather
            # than being suppressed at the component's "warning" default.
            log_level="info",
            application_module="ol_analytics_api.main:app",
        ),
        application_port=APPLICATION_PORT,
        import_nginx_config=False,
        # Not a Django app: no migrations, no collectstatic, no nginx.
        init_migrations=False,
        init_collectstatic=False,
        probe_configs=_probe_configs,
        resource_requests={"cpu": "100m", "memory": "256Mi"},
        resource_limits={"memory": "512Mi"},
        # Pod/container hardening. The app image already declares
        # `USER appuser` (uid/gid 1000) and PYTHONDONTWRITEBYTECODE=1, so
        # nothing writes to the image filesystem at runtime and a read-only
        # root is free -- with the exception of tempfile's default directory,
        # which the emptyDir below keeps writable.
        pod_security_context=kubernetes.core.v1.PodSecurityContextArgs(
            run_as_non_root=True,
            run_as_user=1000,
            run_as_group=1000,
            # An emptyDir is created root-owned; without an fsGroup the app
            # user cannot write into the /tmp mount below.
            fs_group=1000,
            seccomp_profile=kubernetes.core.v1.SeccompProfileArgs(
                type="RuntimeDefault"
            ),
        ),
        container_security_context=kubernetes.core.v1.SecurityContextArgs(
            allow_privilege_escalation=False,
            read_only_root_filesystem=True,
            capabilities=kubernetes.core.v1.CapabilitiesArgs(drop=["ALL"]),
        ),
        extra_volumes=[
            kubernetes.core.v1.VolumeArgs(
                name="tmp",
                empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(
                    size_limit="64Mi"
                ),
            )
        ],
        extra_volume_mounts=[
            kubernetes.core.v1.VolumeMountArgs(
                name="tmp",
                mount_path="/tmp",  # noqa: S108
            )
        ],
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[
            ol_analytics_api_auth_binding,
            # Every Secret named in env_from_secret_names belongs here: the
            # Deployment mounts them via envFrom, so a pod scheduled before
            # the operator has materialized one crash-loops on a missing
            # secret rather than waiting for it.
            static_secrets,
            mitxonline_oauth_secrets,
            ol_analytics_api_application_security_group,
        ],
    ),
)

########################################################################
# APISIX ingress + Keycloak OIDC
########################################################################
ol_analytics_api_domain = ol_analytics_api_config.require("domain")

# Additional host(s) that also route to this service beyond the canonical
# `domain`: the MIT Learn frontend consumes it under a Learn-scoped host
# (`analytics.learn.mit.edu`) alongside the canonical `analytics.ol.mit.edu`.
# Each host gets its own APISIX route with its OWN OIDC resource (see below)
# rather than being folded into one route's host list, so HOST-based tenant
# routing or host-specific plugins can be layered on later without
# restructuring; all hosts share a single SAN certificate. A learn.mit.edu
# host only provisions once `learn.mit.edu` is in the data cluster's
# `allowed_dns_zones` -- both the external-dns domainFilter and the
# cert-manager DNS-01 solver selector read that list (see
# infrastructure/aws/eks and substructure/aws/eks).
ol_analytics_api_learn_domain = ol_analytics_api_config.get("learn_domain")
ol_analytics_api_hosts = [ol_analytics_api_domain]
if ol_analytics_api_learn_domain:
    ol_analytics_api_hosts.append(ol_analytics_api_learn_domain)

# Shared plugins (redirect http->https, cors, prometheus, opentelemetry, ...).
ol_analytics_api_shared_plugins = OLApisixSharedPlugins(
    f"ol-analytics-api-{stack_info.env_suffix}-ol-shared-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name=APPLICATION_NAME,
        resource_suffix="ol-shared-plugins",
        k8s_namespace=APPLICATION_NAMESPACE,
        k8s_labels=k8s_global_labels,
        enable_defaults=True,
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

# OIDC resources: sync the Keycloak client config from Vault and expose the
# openid-connect plugin config.  The `organization:*` scope (the component
# default) is required so the org membership claim reaches X-Userinfo, which the
# b2b_dashboard tenant's auth checks depend on.  This resource, with its own
# dedicated Keycloak client (`ol-analytics-api-client`, see olapps.py), is used
# only for the canonical `analytics.ol.mit.edu` host below with a full
# redirect-to-Keycloak flow.
ol_analytics_api_oidc_resources = OLApisixOIDCResources(
    f"ol-analytics-api-{stack_info.env_suffix}-oidc-resources",
    oidc_config=OLApisixOIDCConfig(
        application_name=APPLICATION_NAME,
        k8s_labels=k8s_global_labels,
        k8s_namespace=APPLICATION_NAMESPACE,
        oidc_logout_path="/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{ol_analytics_api_domain}/",
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/ol-analytics-api",
        vaultauth=ol_analytics_api_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=ol_analytics_api_auth_binding.vault_k8s_resources,
    ),
)

# A second, separate OIDC resource for the Learn-scoped host
# (`analytics.learn.mit.edu` et al.): the MIT Learn frontend calls this API
# with `fetch`, not a browser navigation, so a redirect-to-Keycloak response
# (what the canonical host's "auth" flow above returns) would show up as a
# CORS/opaque-redirect failure, not the JSON 401/403 the frontend expects.
# Rather than minting its own session, this resource reuses mit-learn's own
# Keycloak client (`sso/mitlearn`, same vault_path mit_learn's and learn_ai's
# own OIDC resources read -- see mit_learn/__main__.py and
# learn_ai/__main__.py) so the cookie set when a user logs into mit-learn is
# recognized here too, mirroring how learn-ai's `/ai/*` route piggybacks on
# mit-learn's session rather than the JWT-plugin approach that ask implied
# (this repo has no jwt-auth-plugin route anywhere; the shared-session
# pass-through IS the established pattern). Sharing the session requires two
# things beyond the shared client: `unauth_action="pass"` below (never
# redirects) and a cookie domain broad enough to be sent cross-subdomain --
# set to the same suffix as mit_learn's own broadened
# oidc_session_cookie_domain (mitlearn_api_domain.removeprefix("api")) so the
# exact same browser cookie reaches this host.
ol_analytics_api_learn_oidc_resources = None
if ol_analytics_api_learn_domain:
    ol_analytics_api_learn_oidc_resources = OLApisixOIDCResources(
        f"ol-analytics-api-{stack_info.env_suffix}-learn-oidc-resources",
        oidc_config=OLApisixOIDCConfig(
            application_name=f"{APPLICATION_NAME}-learn",
            k8s_labels=k8s_global_labels,
            k8s_namespace=APPLICATION_NAMESPACE,
            oidc_logout_path="/logout/oidc",
            oidc_post_logout_redirect_uri=f"https://{ol_analytics_api_learn_domain}/",
            oidc_session_absolute_timeout=60 * 20160,
            oidc_session_idling_timeout=0,
            oidc_session_rolling_timeout=0,
            oidc_session_cookie_domain=ol_analytics_api_learn_domain.removeprefix(
                "analytics"
            ),
            # Same reason as the cookie domain above: this host reads the
            # cookie mit-learn's login flow set, so the name has to match
            # mit_learn/__main__.py's oidc_session_cookie_name exactly.
            oidc_session_cookie_name=mit_learn_session_cookie_name(
                stack_info.env_suffix,
            ),
            oidc_use_session_secret=True,
            vault_mount="secret-operations",
            vault_mount_type="kv-v1",
            vault_path="sso/mitlearn",
            vaultauth=ol_analytics_api_auth_binding.vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            parent=ol_analytics_api_auth_binding.vault_k8s_resources,
        ),
    )

# TLS certificate + ApisixTls resource for the service domain.
ol_analytics_api_cert = OLCertManagerCert(
    f"ol-analytics-api-{stack_info.env_suffix}-cert",
    cert_config=OLCertManagerCertConfig(
        application_name=APPLICATION_NAME,
        k8s_namespace=APPLICATION_NAMESPACE,
        k8s_labels=k8s_global_labels,
        create_apisixtls_resource=True,
        dest_secret_name="ol-analytics-api-tls",  # noqa: S106  # pragma: allowlist secret
        dns_names=ol_analytics_api_hosts,
    ),
)

# Route every path to the service behind the openid-connect plugin so APISIX
# authenticates the Keycloak session and forwards X-Userinfo.  The canonical
# host uses unauth_action "auth" (redirects unauthenticated browsers to
# Keycloak); the Learn-scoped host uses "pass" against the shared mit-learn
# session instead (see ol_analytics_api_learn_oidc_resources above for why).
_ol_analytics_api_route_configs = [
    OLApisixRouteConfig(
        route_name="ol-analytics-api",
        priority=10,
        shared_plugin_config_name=ol_analytics_api_shared_plugins.resource_name,
        plugins=[
            OLApisixPluginConfig(
                **ol_analytics_api_oidc_resources.get_full_oidc_plugin_config(
                    unauth_action="auth"
                )
            ),
        ],
        hosts=[ol_analytics_api_domain],
        paths=["/*"],
        backend_service_name=ol_analytics_api_k8s.application_lb_service_name,
        backend_service_port=ol_analytics_api_k8s.application_lb_service_port_name,
        backend_resolve_granularity="service",
    )
]
if ol_analytics_api_learn_oidc_resources is not None:
    _ol_analytics_api_route_configs.append(
        OLApisixRouteConfig(
            route_name="ol-analytics-api-learn",
            priority=10,
            shared_plugin_config_name=ol_analytics_api_shared_plugins.resource_name,
            plugins=[
                OLApisixPluginConfig(
                    **ol_analytics_api_learn_oidc_resources.get_full_oidc_plugin_config(
                        unauth_action="pass"
                    )
                ),
            ],
            hosts=[ol_analytics_api_learn_domain],
            paths=["/*"],
            backend_service_name=ol_analytics_api_k8s.application_lb_service_name,
            backend_service_port=ol_analytics_api_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        )
    )

ol_analytics_api_route = OLApisixRoute(
    f"ol-analytics-api-{stack_info.env_suffix}-route",
    k8s_namespace=APPLICATION_NAMESPACE,
    k8s_labels=k8s_global_labels,
    route_configs=_ol_analytics_api_route_configs,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[
            ol_analytics_api_k8s,
            ol_analytics_api_oidc_resources,
            *(
                [ol_analytics_api_learn_oidc_resources]
                if ol_analytics_api_learn_oidc_resources is not None
                else []
            ),
        ],
    ),
)

export(
    "ol_analytics_api",
    {
        "namespace": APPLICATION_NAMESPACE,
        "domain": ol_analytics_api_domain,
        "service_name": ol_analytics_api_k8s.application_lb_service_name,
        "vault_k8s_auth_role": APPLICATION_NAME,
        "irsa_role_arn": ol_analytics_api_auth_binding.irsa_role.arn,
    },
)
