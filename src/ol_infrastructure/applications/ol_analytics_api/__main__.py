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

* **The workload and routes come from ``definition.py``.**  This program
  binds them to the data cluster (stack references, the pod security group,
  ``OLEKSAuthBinding``, the Vault mounts and VSO secrets) and calls the
  definition, which local-dev renders with k3d bindings
  (``local-dev/apps/ol-analytics-api/pulumi``).

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

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.ol_analytics_api import definition
from ol_infrastructure.applications.ol_analytics_api.vault_secrets import (
    secret_named,
    vault_keys,
    vault_secrets,
)
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCResources,
    OLApisixRoute,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import OLApplicationK8s
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
    parse_stack,
)
from ol_infrastructure.lib.vault import setup_vault_provider

APPLICATION_NAME = definition.APPLICATION_NAME
APPLICATION_NAMESPACE = definition.NAMESPACE

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

starrocks_vault_mount_path = definition.STARROCKS_VAULT_MOUNT

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
#
# Each Vault path below, and the variables it supplies, is described in
# vault_secrets.py, which the local-dev program checks its Secret against.
_vault_secrets = vault_secrets()
_static_vault_secret = secret_named(_vault_secrets, "ol-analytics-api-static-secrets")
_mitxonline_oauth_vault_secret = secret_named(
    _vault_secrets, "ol-analytics-api-mitxonline-oauth"
)
_learner_records_vault_secret = secret_named(
    _vault_secrets, "ol-analytics-api-learner-records-oidc"
)

ol_analytics_api_secrets_mount = vault.Mount(
    f"ol-analytics-api-secrets-mount-{stack_info.env_suffix}",
    path=_static_vault_secret.mount,
    type="kv-v2",
    options={"version": "2"},
    description="Static application secrets for ol-analytics-api (e.g. SENTRY_DSN).",
    opts=ResourceOptions(delete_before_replace=True),
)

ol_analytics_api_static_vault_secrets = vault.generic.Secret(
    f"ol-analytics-api-secrets-{stack_info.env_suffix}",
    path=ol_analytics_api_secrets_mount.path.apply(
        lambda mount: f"{mount}/{_static_vault_secret.path}"
    ),
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
        mount=_static_vault_secret.mount,
        mount_type=_static_vault_secret.mount_type,
        path=_static_vault_secret.path,
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
# org-manager check to MITx Online (mitodl/mitxonline#3807).  The OAuth2
# Application record is created by hand in MITx Online's Django admin.
#
# In CI the credentials come from SOPS and this stack writes them to
# secret-operations, the same way applications/jupyterhub_data writes its crypt
# key.  QA and Production predate that: their entries were written to Vault by
# hand and have no SOPS file yet, so this stack leaves them alone.
#
# The vault-secrets-operator reads the entry at runtime and templates it into the
# env var names the app expects, so the app never sees Vault directly.
#
# Temporary, along with the round-trip it authenticates: once org-manager status
# is visible in the Keycloak token (mitodl/hq#10594), the app stops calling MITx
# Online and this can be deleted along with the Vault entry and the SOPS file.
mitxonline_oauth_depends_on = []
if stack_info.env_suffix == "ci":
    mitxonline_oauth_creds = read_yaml_secrets(
        Path(f"ol_analytics_api/secrets.{stack_info.env_suffix}.yaml")
    )["mitxonline_oauth"]
    mitxonline_oauth_depends_on.append(
        vault.generic.Secret(
            f"ol-analytics-api-mitxonline-oauth-vault-secret-{stack_info.env_suffix}",
            path=(
                f"{_mitxonline_oauth_vault_secret.mount}/"
                f"{_mitxonline_oauth_vault_secret.path}"
            ),
            data_json=Output.secret(
                json.dumps(
                    {
                        "client_id": mitxonline_oauth_creds["client_id"],
                        "client_secret": mitxonline_oauth_creds["client_secret"],
                    }
                )
            ),
        )
    )

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
        mount=_mitxonline_oauth_vault_secret.mount,
        mount_type=_mitxonline_oauth_vault_secret.mount_type,
        path=_mitxonline_oauth_vault_secret.path,
        includes=vault_keys(_mitxonline_oauth_vault_secret),
        excludes=[".*"],
        exclude_raw=True,
        refresh_after="1h",
        templates=_mitxonline_oauth_vault_secret.templates,
        vaultauth=ol_analytics_api_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=ol_analytics_api_auth_binding.vault_k8s_resources,
        depends_on=mitxonline_oauth_depends_on,
    ),
)

# The realm the learner-records tenant verifies partner tokens against, and the
# audience it requires in them. That tenant verifies the bearer JWT itself
# rather than trusting the X-Userinfo APISIX forwards, because the pod is
# reachable without going through APISIX: the pod security group admits the
# whole pod subnet, and aws-eks-nodeagent runs with
# --enable-network-policy=false on both data clusters, so the NetworkPolicies
# that exist are no-ops.  See the app repo's
# docs/b2b-learner-records-provider-authorization.md.
#
# Both values come from the same Vault entry the route's openid-connect plugin
# reads for its discovery URL and client id, so the gateway and the app cannot
# end up checking different realms or a different audience.  The app derives
# its JWKS and token URLs from the issuer.  Templated by the operator at
# runtime for the same reason the MITx Online pair above is: the app takes
# these as plain env vars, and pulling them into Pulumi with
# get_secret_output would put the realm URL in stack state for no gain.
learner_records_oidc_secret_name = (
    "ol-analytics-api-learner-records-oidc"  # pragma: allowlist secret  # noqa: S105
)
learner_records_oidc_secrets = OLVaultK8SSecret(
    name=f"ol-analytics-api-{stack_info.env_suffix}-learner-records-oidc-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=learner_records_oidc_secret_name,
        namespace=APPLICATION_NAMESPACE,
        labels=k8s_global_labels,
        dest_secret_name=learner_records_oidc_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount=_learner_records_vault_secret.mount,
        mount_type=_learner_records_vault_secret.mount_type,
        path=_learner_records_vault_secret.path,
        includes=vault_keys(_learner_records_vault_secret),
        excludes=[".*"],
        exclude_raw=True,
        refresh_after="1h",
        templates=_learner_records_vault_secret.templates,
        vaultauth=ol_analytics_api_auth_binding.vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=ol_analytics_api_auth_binding.vault_k8s_resources,
    ),
)

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
ol_analytics_api_domain = ol_analytics_api_config.require("domain")
# The MIT Learn frontend consumes the service under a Learn-scoped host
# (`analytics.learn.mit.edu`) beside the canonical `analytics.ol.mit.edu`. Each
# host gets its own APISIX route with its OWN OIDC resource rather than being
# folded into one route's host list, so HOST-based tenant routing or
# host-specific plugins can be layered on later without restructuring; all
# hosts share a single SAN certificate. A learn.mit.edu host only provisions
# once `learn.mit.edu` is in the data cluster's `allowed_dns_zones` -- both the
# external-dns domainFilter and the cert-manager DNS-01 solver selector read
# that list (see infrastructure/aws/eks and substructure/aws/eks).
ol_analytics_api_learn_domain = ol_analytics_api_config.get("learn_domain")

bindings = definition.OlAnalyticsApiBindings(
    env_suffix=stack_info.env_suffix,
    hostnames=definition.OlAnalyticsApiHostnames(
        canonical=ol_analytics_api_domain,
        learn=ol_analytics_api_learn_domain,
    ),
    # Non-sensitive config; the Vault login settings below depend on stack
    # outputs.
    environment_variables=dict(ol_analytics_api_config.require_object("env_vars")),
    # Vault itself is the single shared per-env-tier server, same as every other
    # app. What differs per cluster is which Kubernetes auth *mount* on it the
    # pod logs into, since Vault's kubernetes auth method needs each cluster's
    # own API endpoint/CA/JWT issuer. The data cluster's mount is `k8s-data`
    # (env_prefix based, NOT `k8s-data-{env}`), read from the cluster stack
    # rather than hardcoded.
    starrocks_vault=definition.StarRocksVaultLogin(
        address=vault_config.require("address"),
        k8s_auth_mount=cluster_stack.require_output("vault_auth_endpoint"),
    ),
    # Browser origins outside the canonical host that call it with credentials,
    # named per stack.
    cors_frontend_origins=ol_analytics_api_config.get_object("cors_frontend_origins")
    or [],
    k8s_labels=k8s_global_labels,
    secret_names=[
        static_secrets_name,
        mitxonline_oauth_secret_name,
        learner_records_oidc_secret_name,
    ],
    cluster=definition.ClusterCapabilities(
        security_group_id=ol_analytics_api_application_security_group.id,
        security_group_name=Output.from_input(APPLICATION_NAME),
        vault_auth_name=ol_analytics_api_auth_binding.vault_k8s_resources.auth_name,
    ),
    service_account_name=APPLICATION_NAME,
    **docker_image_config_kwargs("OL_ANALYTICS_API"),
    min_replicas=ol_analytics_api_config.get_int("min_replicas") or 2,
)

ol_analytics_api_k8s = OLApplicationK8s(
    ol_app_k8s_config=definition.application_config(bindings),
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
            learner_records_oidc_secrets,
            ol_analytics_api_application_security_group,
        ],
    ),
)

########################################################################
# APISIX ingress + Keycloak OIDC
########################################################################
# Shared plugins (redirect http->https, cors, prometheus, opentelemetry, ...).
ol_analytics_api_shared_plugins = OLApisixSharedPlugins(
    f"ol-analytics-api-{stack_info.env_suffix}-ol-shared-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name=APPLICATION_NAME,
        resource_suffix="ol-shared-plugins",
        k8s_namespace=APPLICATION_NAMESPACE,
        k8s_labels=k8s_global_labels,
        enable_defaults=True,
        cors_allow_origins=definition.cors_allow_origins(bindings),
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

# The canonical host's OIDC resources, syncing its dedicated Keycloak client
# (`ol-analytics-api-client`, see olapps.py) from Vault.
ol_analytics_api_oidc_resources = OLApisixOIDCResources(
    f"ol-analytics-api-{stack_info.env_suffix}-oidc-resources",
    oidc_config=definition.canonical_oidc_config(bindings),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=ol_analytics_api_auth_binding.vault_k8s_resources,
    ),
)

# A second, separate OIDC resource for the Learn-scoped host, sharing
# mit-learn's session (see definition.learn_oidc_config for why).
ol_analytics_api_learn_oidc_resources = None
if ol_analytics_api_learn_domain:
    ol_analytics_api_learn_oidc_resources = OLApisixOIDCResources(
        f"ol-analytics-api-{stack_info.env_suffix}-learn-oidc-resources",
        oidc_config=definition.learn_oidc_config(bindings),
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
        dns_names=definition.hosts(bindings),
    ),
)

_ol_analytics_api_route_configs = definition.canonical_route_configs(
    bindings,
    ol_analytics_api_oidc_resources,
    shared_plugin_config_name=ol_analytics_api_shared_plugins.resource_name,
)
if ol_analytics_api_learn_oidc_resources is not None:
    _ol_analytics_api_route_configs.append(
        definition.learn_route_config(
            bindings,
            ol_analytics_api_learn_oidc_resources,
            shared_plugin_config_name=ol_analytics_api_shared_plugins.resource_name,
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
