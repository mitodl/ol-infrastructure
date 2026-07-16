"""Canonical Pulumi project name constants for cross-project stack references.

Use these constants with
:func:`ol_infrastructure.lib.pulumi_helper.make_stack_reference`
to build StackReference objects:

.. code-block:: python

    from ol_infrastructure.lib import pulumi_projects as projects
    from ol_infrastructure.lib.pulumi_helper import make_stack_reference

    network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
    eks_stack = make_stack_reference(
        projects.EKS_SUB, f"{stack_info.env_prefix}.{stack_info.name}"
    )
"""

# ---------------------------------------------------------------------------
# infrastructure/
# ---------------------------------------------------------------------------

NETWORKING = "ol-infrastructure-networking"
KMS = "ol-infrastructure-aws-kms"
DNS = "ol-infrastructure-aws-dns"
IAM = "ol-infrastructure-aws-iam"
ECR = "ol-infrastructure-ecr"
PRIVATE_CA = "ol-infrastructure-private-ca"
POLICIES = "ol-infrastructure-aws-policies"
EKS = "ol-infrastructure-eks"
OPENSEARCH = "ol-infrastructure-opensearch"
VAULT_SERVER = "ol-infrastructure-vault-server"
CONSUL_INFRA = "ol-infrastructure-consul"
MONITORING = "ol-infrastructure-monitoring"
VECTOR_LOG_PROXY = "ol-infrastructure-vector-log-proxy"
DATA_WAREHOUSE = "ol-infrastructure-data-warehouse"
MONGODB_ATLAS = "ol-infrastructure-mongodb-atlas"
QDRANT_CLOUD = "ol-infrastructure-qdrant-cloud"
GRAFANA_CLOUD = "ol-infrastructure-grafana-cloud"
GRAFANA_ALERTING = "ol-infrastructure-grafana-alerting"
SENTRY = "ol-infrastructure-sentry"
SFTP = "ol-infrastructure-aws-sftp"
S3_SITES = "ol-infrastructure-aws-s3"
GEMINI_API = "ol-infrastructure-gemini-api"

# ---------------------------------------------------------------------------
# substructure/
# ---------------------------------------------------------------------------

EKS_SUB = "ol-substructure-eks"
VAULT_AUTH = "ol-substructure-vault-auth"
VAULT_SETUP = "ol-substructure-vault-setup"
VAULT_STATIC_MOUNTS = "ol-substructure-vault-static-mounts"
VAULT_ENCRYPTION_MOUNTS = "ol-substructure-vault-encryption-mounts"
VAULT_PKI = "ol-substructure-vault-pki"
VAULT_APPROLES = "ol-substructure-vault-approles"
VAULT_SECRETS = "ol-substructure-vault-secrets"  # pragma: allowlist secret
CONSUL_SUB = "ol-substructure-consul"
KEYCLOAK_SUB = "ol-substructure-keycloak"
OPEN_METADATA_SUB = "ol-substructure-open-metadata"
STARROCKS_SUB = "ol-substructure-starrocks"
TLS_CERTS = "ol-substructure-tls-certificates"
XPRO_PARTNER_DNS = "ol-substructure-xpro-partner-dns"

# ---------------------------------------------------------------------------
# applications/
# ---------------------------------------------------------------------------

MIT_LEARN = "ol-application-mit-learn"
EDXAPP = "ol-application-edxapp"
EDX_NOTES = "ol-application-edx-notes"
CODEJAIL = "ol-application-codejail"
CONCOURSE = "ol-application-concourse"
AIRBYTE = "ol-application-airbyte"
DAGSTER = "ol-application-dagster"
OCW_STUDIO = "ol-application-ocw-studio"
JUPYTERHUB = "ol-application-jupyterhub"
JUPYTERHUB_DATA = "ol-application-jupyterhub-data"
MARIMO_DATA = "ol-application-marimo-data"
SUPERSET = "ol-application-superset"
OPEN_METADATA = "ol-application-open-metadata"
OPENLIT = "ol-application-openlit"
KUBEWATCH = "ol-application-kubewatch"
KUBEWATCH_WEBHOOK = "ol-application-kubewatch-webhook"
FASTLY_REDIRECTOR = "ol-application-fastly-redirector"
CELERY_MONITORING = "ol-application-celery-monitoring"
LEARN_AI = "ol-application-learn-ai"
MICROMASTERS = "ol-application-micromasters"
OL_ANALYTICS_API = "ol-application-ol-analytics-api"
MITXONLINE = "ol-application-mitxonline"
B2B_STORAGE = "ol-application-b2b-partners-storage"
DIGITAL_CREDENTIALS = "ol-application-digital-credentials"
STARBURST = "ol-application-starburst"
ECS_TEST = "ol-application-ecs-test"
BOOTCAMPS = "ol-application-bootcamps"
OCW_SITE = "ol-application-ocw-site"
MIT_LEARN_NEXTJS = "ol-application-mit-learn-nextjs"
CLICKHOUSE = "ol-application-clickhouse"
KEYCLOAK_APP = "ol-application-keycloak"
MAILGUN = "ol-application-mailgun"
MITX = "ol-application-mitx"
ODL_VIDEO_SERVICE = "ol-application-odl-video-service"
OPEN_DISCUSSIONS = "ol-application-open-discussions"
STARROCKS_APP = "ol-application-starrocks"
TIKA = "ol-application-tika"
XPRO = "ol-application-xpro"
XQUEUE = "ol-application-xqueue"
XQWATCHER = "ol-application-xqwatcher"
TOOLHIVE_OPERATOR = "ol-application-toolhive-operator"
TOOLHIVE_SWE = "ol-application-toolhive-swe"
TOOLHIVE_APPS = "ol-application-toolhive-apps"
TOOLHIVE_DATA = "ol-application-toolhive-data"

# ---------------------------------------------------------------------------
# LEGACY_STACK_REF_PREFIXES — alias shim for post-migration state reconciliation
#
# Maps each project-name constant to the legacy flat-namespace dotted prefix
# that was used before the project-scoped stack migration.
#
# :func:`ol_infrastructure.lib.pulumi_helper.make_stack_reference` uses this
# table to emit a ``pulumi.Alias`` so that Pulumi can match old StackReference
# resource entries in the state file (which still carry the legacy name) to the
# new project-scoped resource names produced by ``stack_ref()``.  Without the
# alias Pulumi tries to delete the old resources (reading from the now-gone
# legacy stacks) and create fresh ones — which fails with "unknown stack".
#
# This mapping MUST remain in place until every application stack has been
# successfully updated with the new names (i.e., until the legacy names no
# longer appear in any stack's state file).
# ---------------------------------------------------------------------------

LEGACY_STACK_REF_PREFIXES: dict[str, str] = {
    # infrastructure/
    NETWORKING: "infrastructure.aws.network",
    KMS: "infrastructure.aws.kms",
    DNS: "infrastructure.aws.dns",
    IAM: "infrastructure.aws.iam",
    ECR: "infrastructure.aws.ecr",
    PRIVATE_CA: "infrastructure.aws.private_ca",
    POLICIES: "infrastructure.aws.policies",
    EKS: "infrastructure.aws.eks",
    OPENSEARCH: "infrastructure.aws.opensearch",
    VAULT_SERVER: "infrastructure.vault",
    CONSUL_INFRA: "infrastructure.consul",
    MONITORING: "infrastructure.monitoring",
    VECTOR_LOG_PROXY: "infrastructure.vector_log_proxy",
    DATA_WAREHOUSE: "infrastructure.aws.data_warehouse",
    MONGODB_ATLAS: "infrastructure.mongodb_atlas",
    QDRANT_CLOUD: "infrastructure.qdrant_cloud",
    GRAFANA_CLOUD: "infrastructure.grafana_cloud",
    SENTRY: "infrastructure.sentry",
    SFTP: "infrastructure.aws.sftp_servers",
    S3_SITES: "infrastructure.aws.s3_sites",
    # substructure/
    EKS_SUB: "substructure.aws.eks",
    VAULT_AUTH: "substructure.vault.auth",
    VAULT_SETUP: "substructure.vault.setup",
    VAULT_STATIC_MOUNTS: "substructure.vault.static_mounts",
    VAULT_ENCRYPTION_MOUNTS: "substructure.vault.encryption_mounts",
    VAULT_PKI: "substructure.vault.pki",
    VAULT_APPROLES: "substructure.vault.approles",
    VAULT_SECRETS: "substructure.vault.secrets",  # pragma: allowlist secret
    CONSUL_SUB: "substructure.consul",
    KEYCLOAK_SUB: "substructure.keycloak",
    OPEN_METADATA_SUB: "substructure.open_metadata",
    STARROCKS_SUB: "substructure.starrocks",
    TLS_CERTS: "substructure.tls_certificates",
    XPRO_PARTNER_DNS: "substructure.xpro_partner_dns",
    # applications/
    MIT_LEARN: "applications.mit_learn",
    EDXAPP: "applications.edxapp",
    EDX_NOTES: "applications.edxnotes",
    CODEJAIL: "applications.codejail",
    CONCOURSE: "applications.concourse",
    AIRBYTE: "applications.airbyte",
    DAGSTER: "applications.dagster",
    OCW_STUDIO: "applications.ocw_studio",
    JUPYTERHUB: "applications.jupyterhub",
    SUPERSET: "applications.superset",
    OPEN_METADATA: "applications.open_metadata",
    KUBEWATCH: "applications.kubewatch",
    KUBEWATCH_WEBHOOK: "applications.kubewatch_webhook_handler",
    FASTLY_REDIRECTOR: "applications.fastly_redirector",
    CELERY_MONITORING: "applications.celery_monitoring",
    LEARN_AI: "applications.learn_ai",
    MICROMASTERS: "applications.micromasters",
    MITXONLINE: "applications.mitxonline",
    B2B_STORAGE: "applications.b2b_partners_storage",
    DIGITAL_CREDENTIALS: "applications.digital_credentials",
    STARBURST: "applications.starburst",
    BOOTCAMPS: "applications.bootcamps",
    OCW_SITE: "applications.ocw_site",
    MIT_LEARN_NEXTJS: "applications.mit_learn_nextjs",
    CLICKHOUSE: "applications.clickhouse",
    KEYCLOAK_APP: "applications.keycloak",
    MAILGUN: "applications.mailgun",
    MITX: "applications.mitx",
    ODL_VIDEO_SERVICE: "applications.odl_video_service",
    OPEN_DISCUSSIONS: "applications.open_discussions",
    STARROCKS_APP: "applications.starrocks",
    TIKA: "applications.tika",
    XPRO: "applications.xpro",
    XQUEUE: "applications.xqueue",
    XQWATCHER: "applications.xqwatcher",
}
