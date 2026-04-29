"""Canonical Pulumi project name constants for cross-project stack references.

Use these constants with :func:`ol_infrastructure.lib.pulumi_helper.stack_ref`
to build stack reference strings:

.. code-block:: python

    from ol_infrastructure.lib import pulumi_projects as projects
    from ol_infrastructure.lib.pulumi_helper import stack_ref

    network_stack = StackReference(stack_ref(projects.NETWORKING, stack_info.name))
    eks_stack = StackReference(
        stack_ref(projects.EKS_SUB, f"{stack_info.env_prefix}.{stack_info.name}")
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
SUPERSET = "ol-application-superset"
OPEN_METADATA = "ol-application-open-metadata"
KUBEWATCH = "ol-application-kubewatch"
KUBEWATCH_WEBHOOK = "ol-application-kubewatch-webhook"
FASTLY_REDIRECTOR = "ol-application-fastly-redirector"
CELERY_MONITORING = "ol-application-celery-monitoring"
REDASH = "ol-application-redash"
LEARN_AI = "ol-application-learn-ai"
MICROMASTERS = "ol-application-micromasters"
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
