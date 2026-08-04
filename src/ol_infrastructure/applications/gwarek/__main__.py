"""Gwarek application deployment.

Security-posture dashboard: FastAPI + ARQ worker (sharing one image) plus a
Next.js frontend (a separate image), a dedicated RDS Postgres + ElastiCache
Redis (via Vault dynamic DB credentials), S3 buckets for analyzed reports /
raw snapshots / LLM wiki grounding docs, and an APISIX route with the
openid-connect plugin in front for Keycloak SSO.

Single environment (Production) by design — this is an internal tool with a
local-test-then-manually-deploy workflow, not the org's full CI/QA/Production
Concourse promotion pipeline used by larger apps.
"""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, Output, ResourceOptions
from pulumi_aws import ec2, get_caller_identity
from pulumi_kubernetes import batch, core, meta
from pulumi_kubernetes.apps import v1 as apps_v1

from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultPostgresDatabaseConfig,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
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
    format_docker_image_ref,
    make_stack_reference,
    parse_stack,
)
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)
gwarek_config = Config("gwarek")
redis_config = Config("redis")
aws_account = get_caller_identity()

network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
vault_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
)
operations_vpc = network_stack.require_output("operations_vpc")

cluster_stack = make_stack_reference(projects.EKS, f"operations.{stack_info.name}")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

gwarek_namespace = "gwarek"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(gwarek_namespace, ns)
)

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"operations-{stack_info.env_suffix}"}
)

k8s_global_labels = K8sGlobalLabels(
    service=Services.gwarek,
    ou=BusinessUnit.operations,
    stack=stack_info,
)
application_labels = {**k8s_global_labels.model_dump(), "app": "gwarek"}

gwarek_domain = gwarek_config.require("domain")
k8s_pod_subnet_cidrs = operations_vpc["k8s_pod_subnet_cidrs"]

# ---------------------------------------------------------------------------
# Pod security group
# ---------------------------------------------------------------------------
gwarek_app_security_group = ec2.SecurityGroup(
    f"gwarek-app-access-{stack_info.env_suffix}",
    description=f"Access control for the Gwarek app in {stack_info.name}",
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
    tags=aws_config.tags,
    vpc_id=operations_vpc["id"],
)

# The security group above only takes effect on pods the VPC CNI actually
# associates it with, via this SecurityGroupPolicy binding by pod label —
# OLApplicationK8s creates this automatically for apps that use it, but
# this project uses plain Deployments, so it must be created explicitly.
# Pods needing the DB/Redis ingress rules that reference this security
# group (api, worker) carry POD_SECURITY_GROUP_LABEL below; web does not.
POD_SECURITY_GROUP_LABEL_KEY = "ol.mit.edu/pod-security-group"
POD_SECURITY_GROUP_LABEL_VALUE = "gwarek-app"
POD_SECURITY_GROUP_LABEL = {
    POD_SECURITY_GROUP_LABEL_KEY: POD_SECURITY_GROUP_LABEL_VALUE
}
gwarek_app_security_group_policy = kubernetes.apiextensions.CustomResource(
    f"gwarek-app-security-group-policy-{stack_info.env_suffix}",
    api_version="vpcresources.k8s.aws/v1beta1",
    kind="SecurityGroupPolicy",
    metadata=meta.v1.ObjectMetaArgs(
        name=POD_SECURITY_GROUP_LABEL_VALUE,
        namespace=gwarek_namespace,
        labels=application_labels,
    ),
    spec={
        "podSelector": {"matchLabels": POD_SECURITY_GROUP_LABEL},
        "securityGroups": {"groupIds": [gwarek_app_security_group.id]},
    },
)

# ---------------------------------------------------------------------------
# RDS Postgres + Vault dynamic DB credentials
# ---------------------------------------------------------------------------
gwarek_db_security_group = ec2.SecurityGroup(
    f"gwarek-db-access-{stack_info.env_suffix}",
    description=f"Access control for the Gwarek DB in {stack_info.name}",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=5432,
            to_port=5432,
            security_groups=[
                vault_stack.require_output("vault_server")["security_group"],
            ],
            description="Allow access from vault servers for secrets management",
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=5432,
            to_port=5432,
            cidr_blocks=k8s_pod_subnet_cidrs,
            description="Allow access from the app running in Kubernetes",
        ),
    ],
    egress=[
        ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    tags=aws_config.tags,
    vpc_id=operations_vpc["id"],
)

rds_defaults = defaults(stack_info)["rds"]
gwarek_db_config = OLPostgresDBConfig(
    instance_name=f"gwarek-db-{stack_info.env_suffix}",
    # .require(), not .require_secret() -- OLPostgresDBConfig.password is a
    # plain pydantic SecretStr, not Output-aware; .require() still reads
    # the encrypted config value correctly, just returns a resolved str
    # instead of wrapping it as a Pulumi Output. Matches ocw_studio's
    # identical db_password usage.
    password=gwarek_config.require("db_password"),
    subnet_group_name=operations_vpc["rds_subnet"],
    security_groups=[gwarek_db_security_group],
    engine_major_version="18",
    tags=aws_config.tags,
    db_name="gwarek",
    public_access=False,
    **rds_defaults,
)
gwarek_db = OLAmazonDB(gwarek_db_config)

gwarek_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=gwarek_db_config.db_name,
    mount_point=f"{gwarek_db_config.engine}-gwarek-{stack_info.env_suffix}",
    db_admin_username=gwarek_db_config.username,
    db_admin_password=gwarek_db_config.password.get_secret_value(),
    db_host=gwarek_db.db_instance.address,
)
gwarek_vault_backend = OLVaultDatabaseBackend(gwarek_vault_backend_config)

# ---------------------------------------------------------------------------
# ElastiCache Redis
# ---------------------------------------------------------------------------
redis_cluster_security_group = ec2.SecurityGroup(
    f"gwarek-redis-cluster-security-group-{stack_info.env_suffix}",
    name_prefix=f"gwarek-redis-cluster-sg-{stack_info.env_suffix}",
    description="Access control for the Gwarek redis cluster.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[gwarek_app_security_group.id],
            protocol="tcp",
            from_port=6379,
            to_port=6379,
            description="Allow application pods to talk to Redis",
        ),
    ],
    vpc_id=operations_vpc["id"],
    tags=aws_config.tags,
)
redis_defaults = defaults(stack_info)["redis"]
gwarek_redis_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_config.require("password"),
    cluster_mode_enabled=False,
    encrypted=True,
    engine_version="7.2",
    engine="valkey",
    # OLAmazonCache hardcodes automatic_failover_enabled=True (not
    # configurable), which AWS requires at least 2 cache nodes for --
    # num_instances=1 fails at apply time with "must be at least 2 if
    # automatic_failover_enabled is true". 2 is the minimum compliant value.
    num_instances=2,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for Gwarek",
    cluster_name=f"gwarek-redis-{stack_info.env_suffix}",
    subnet_group=operations_vpc["elasticache_subnet"],
    security_groups=[redis_cluster_security_group.id],
    tags=aws_config.tags,
    **redis_defaults,
)
gwarek_redis = OLAmazonCache(gwarek_redis_config)

# ---------------------------------------------------------------------------
# S3 buckets — storage (analyzed reports), snapshots (raw collected data),
# wiki (LLM grounding docs)
# ---------------------------------------------------------------------------
gwarek_storage_bucket_name = f"ol-gwarek-storage-{stack_info.env_suffix}"
gwarek_snapshot_bucket_name = f"ol-gwarek-snapshots-{stack_info.env_suffix}"
gwarek_wiki_bucket_name = f"ol-gwarek-wiki-{stack_info.env_suffix}"

gwarek_storage_bucket = OLBucket(
    "gwarek-storage-bucket",
    config=S3BucketConfig(
        bucket_name=gwarek_storage_bucket_name,
        versioning_enabled=True,
        tags=aws_config.tags,
    ),
)
gwarek_snapshot_bucket = OLBucket(
    "gwarek-snapshot-bucket",
    config=S3BucketConfig(
        bucket_name=gwarek_snapshot_bucket_name,
        versioning_enabled=False,
        tags=aws_config.tags,
    ),
)
gwarek_wiki_bucket = OLBucket(
    "gwarek-wiki-bucket",
    config=S3BucketConfig(
        bucket_name=gwarek_wiki_bucket_name,
        versioning_enabled=True,
        tags=aws_config.tags,
    ),
)

# ---------------------------------------------------------------------------
# EKS auth binding: IRSA for S3 access (api/worker pods talk to S3 directly
# via boto3, not through Vault dynamic AWS creds) + Vault K8s auth
# ---------------------------------------------------------------------------
gwarek_irsa_service_account_name = "gwarek"


def _bucket_arns(bucket_name: str) -> list[str]:
    # Built from the plain bucket name string, not the OLBucket resource's
    # .bucket_v2.arn Output -- S3 ARNs are fully deterministic from the name
    # alone, and OLEKSAuthBinding's iam_policy_document is passed straight
    # to lint_iam_policy's json.dumps(), which cannot serialize an
    # unresolved Output. Mirrors ocw_studio's own IAM policy, which builds
    # its bucket ARN the same way for the same reason.
    return [f"arn:aws:s3:::{bucket_name}", f"arn:aws:s3:::{bucket_name}/*"]


gwarek_iam_policy_document = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:ListBucket",
            ],
            "Resource": [
                *_bucket_arns(gwarek_storage_bucket_name),
                *_bucket_arns(gwarek_snapshot_bucket_name),
                *_bucket_arns(gwarek_wiki_bucket_name),
            ],
        },
        {
            # LLM_BACKEND=bedrock (api/src/pipeline/analyzers/llm.py) uses
            # IAM/IRSA auth instead of a static ANTHROPIC_API_KEY. Scoped to
            # Anthropic foundation models/inference profiles only, not a
            # specific model ID, so bumping the app's configured model
            # doesn't require an infra change too.
            #
            # Claude Sonnet 5 (and other newer models) can't be invoked by
            # bare foundation-model ID on on-demand throughput -- Bedrock
            # requires a cross-Region inference profile ID/ARN instead
            # (BEDROCK_MODEL_ID=us.anthropic.claude-sonnet-5). Per AWS's own
            # docs, granting an inference-profile resource ARN additionally
            # requires granting the underlying foundation-model ARN in
            # *every* Region the profile can route to -- a region wildcard
            # covers that without hardcoding the profile's current
            # us-east-1/us-east-2/us-west-2 destination list, which AWS can
            # change.
            "Effect": "Allow",
            "Action": [
                "bedrock:InvokeModel",
                "bedrock:InvokeModelWithResponseStream",
            ],
            "Resource": [
                "arn:aws:bedrock:*::foundation-model/anthropic.*",
                f"arn:aws:bedrock:*:{aws_account.account_id}:inference-profile/*anthropic*",
            ],
        },
    ],
}

gwarek_auth_binding = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="gwarek",
        namespace=gwarek_namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=gwarek_iam_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath("gwarek_server_policy.hcl"),
        cluster_name=cluster_stack.require_output("cluster_name"),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=gwarek_irsa_service_account_name,
        create_irsa_service_account=True,
        vault_sync_service_account_names=["gwarek-vault"],
        k8s_labels=k8s_global_labels,
        # Parliament's RESOURCE_MISMATCH flags bedrock:InvokeModel* for not
        # also covering every ARN type the action supports (e.g.
        # custom-model-deployment, provisioned-model) -- those don't apply
        # here; foundation-model and inference-profile are the only two
        # actually invoked (see gwarek_iam_policy_document above). An
        # ignore_locations/actions filter
        # would be more surgical, but _is_parliament_finding_filtered
        # (lib/aws/iam_helper.py) indexes finding.location["actions"]
        # unconditionally, and RESOURCE_MISMATCH findings don't carry that
        # key -- KeyError. Blanket-suppressing via an empty dict avoids
        # that code path entirely; mirrors ocw_studio's identical
        # "RESOURCE_EFFECTIVELY_STAR": {} suppression for the same reason.
        parliament_config={"RESOURCE_MISMATCH": {}},
    )
)
gwarek_vaultauth = gwarek_auth_binding.vault_k8s_resources.auth_name

# ---------------------------------------------------------------------------
# Vault-managed secrets -> Kubernetes Secrets
# ---------------------------------------------------------------------------

# Static app secrets (local Fernet key for credential encryption — see
# api/src/pipeline/crypto.py's LocalCrypto). Values are set via
# `pulumi config set --secret` against this stack, not hardcoded here.
# anthropic_api_key is optional: production uses LLM_BACKEND=bedrock
# (IAM/IRSA auth, no static key) -- see the Bedrock IAM statement below --
# and only needs a real key set here if switched back to the direct API.
gwarek_secrets_mount = vault.Mount(
    "gwarek-vault-secrets-storage",
    path="secret-gwarek",
    type="kv-v2",
    description="Static secrets storage for the Gwarek application",
)
gwarek_anthropic_api_key = gwarek_config.get_secret(
    "anthropic_api_key"
) or Output.from_input("")
vault.generic.Secret(
    "gwarek-vault-secrets",
    path=gwarek_secrets_mount.path.apply("{}/collected".format),
    data_json=Output.all(
        anthropic_api_key=gwarek_anthropic_api_key,
        secret_key=gwarek_config.require_secret("secret_key"),
    ).apply(json.dumps),
)

gwarek_app_secrets_k8s_name = "gwarek-app-secrets"  # pragma: allowlist secret
gwarek_app_secrets = OLVaultK8SSecret(
    f"gwarek-app-secrets-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name=gwarek_app_secrets_k8s_name,
        namespace=gwarek_namespace,
        labels=application_labels,
        dest_secret_name=gwarek_app_secrets_k8s_name,
        dest_secret_labels=application_labels,
        mount="secret-gwarek",
        mount_type="kv-v2",
        path="collected",
        excludes=[".*"],
        exclude_raw=True,
        templates={
            "ANTHROPIC_API_KEY": '{{ get .Secrets "anthropic_api_key" }}',
            "SECRET_KEY": '{{ get .Secrets "secret_key" }}',
        },
        vaultauth=gwarek_vaultauth,
    ),
)

# Dynamic Postgres credentials — "app" role for the running api/worker pods.
#
# These templates deliberately emit only DB_USERNAME/DB_PASSWORD (plain
# Vault Go-template expressions, no Pulumi Output involved) rather than a
# single DATABASE_URL embedding the RDS host. OLVaultK8SSecret's Vault
# Secrets Operator manifest builder calls str() on each template value
# (components/services/vault.py); since gwarek_db.db_instance.address is a
# real Output[str] not known until the RDS instance exists, str()-ing an
# .apply() over it would bake Pulumi's "calling __str__ on an Output is not
# supported" diagnostic into the rendered secret instead of the resolved
# host. That's a bug in the shared component (used by many other apps), not
# fixable safely from here, so DB_HOST is instead assembled below as a
# plain container env var — which is a genuine typed Pulumi resource
# argument and so handles the Output correctly — and DATABASE_URL is
# composed from DB_USERNAME/DB_PASSWORD/DB_HOST via Kubernetes' own
# dependent-env-var $(VAR) substitution (see gwarek_db_env below).
gwarek_db_endpoint = gwarek_db.db_instance.address
gwarek_db_app_secret_k8s_name = "gwarek-db-app-creds"  # noqa: S105  # pragma: allowlist secret
gwarek_db_app_secret = OLVaultK8SSecret(
    f"gwarek-db-app-creds-{stack_info.env_suffix}",
    resource_config=OLVaultK8SDynamicSecretConfig(
        name=gwarek_db_app_secret_k8s_name,
        namespace=gwarek_namespace,
        labels=application_labels,
        dest_secret_name=gwarek_db_app_secret_k8s_name,
        dest_secret_labels=application_labels,
        mount=gwarek_vault_backend_config.mount_point,
        path="creds/app",
        excludes=[".*"],
        exclude_raw=True,
        templates={
            "DB_USERNAME": '{{ get .Secrets "username" }}',
            "DB_PASSWORD": '{{ get .Secrets "password" }}',
        },
        vaultauth=gwarek_vaultauth,
        # Kubernetes does not update a running container's env vars when the
        # backing Secret changes, so without this, pods would keep using
        # stale DB credentials after VSO rotates the Vault-issued lease
        # until manually restarted.
        restart_targets=[
            OLVaultRestartTarget(kind="Deployment", name="gwarek-api"),
            OLVaultRestartTarget(kind="Deployment", name="gwarek-worker"),
        ],
    ),
)

# Dynamic Postgres credentials — "admin" role for the one-off migration Job
# only (alembic upgrade head needs DDL privileges the app role lacks). Same
# DB_USERNAME/DB_PASSWORD-only shape and same reason as gwarek_db_app_secret
# above.
gwarek_db_admin_secret_k8s_name = "gwarek-db-admin-creds"  # noqa: S105  # pragma: allowlist secret
gwarek_db_admin_secret = OLVaultK8SSecret(
    f"gwarek-db-admin-creds-{stack_info.env_suffix}",
    resource_config=OLVaultK8SDynamicSecretConfig(
        name=gwarek_db_admin_secret_k8s_name,
        namespace=gwarek_namespace,
        labels=application_labels,
        dest_secret_name=gwarek_db_admin_secret_k8s_name,
        dest_secret_labels=application_labels,
        mount=gwarek_vault_backend_config.mount_point,
        path="creds/admin",
        excludes=[".*"],
        exclude_raw=True,
        templates={
            "DB_USERNAME": '{{ get .Secrets "username" }}',
            "DB_PASSWORD": '{{ get .Secrets "password" }}',
        },
        vaultauth=gwarek_vaultauth,
    ),
)


def _db_env(secret_k8s_name: str) -> list[core.v1.EnvVarArgs]:
    """Env entries composing DATABASE_URL from a DB secret + the RDS host.

    Must be used via `env=`, not `env_from=` — Kubernetes' $(VAR)
    dependent-variable substitution only expands references to variables
    that appear earlier in the same container's explicit `env:` list, not
    ones merged in from `envFrom:`. DB_HOST is gwarek_db_endpoint (an
    Output[str]) passed directly as this EnvVarArgs' `value`, a real typed
    Pulumi resource argument, so it doesn't hit the str()-on-Output bug
    described above.
    """
    return [
        core.v1.EnvVarArgs(
            name="DB_USERNAME",
            value_from=core.v1.EnvVarSourceArgs(
                secret_key_ref=core.v1.SecretKeySelectorArgs(
                    name=secret_k8s_name, key="DB_USERNAME"
                )
            ),
        ),
        core.v1.EnvVarArgs(
            name="DB_PASSWORD",
            value_from=core.v1.EnvVarSourceArgs(
                secret_key_ref=core.v1.SecretKeySelectorArgs(
                    name=secret_k8s_name, key="DB_PASSWORD"
                )
            ),
        ),
        core.v1.EnvVarArgs(name="DB_HOST", value=gwarek_db_endpoint),
        core.v1.EnvVarArgs(
            name="DATABASE_URL",
            value="postgresql+asyncpg://$(DB_USERNAME):$(DB_PASSWORD)@$(DB_HOST)/gwarek",
        ),
    ]


# Redis connection details — plain K8s Secret (matches ocw_studio's
# redis-creds pattern; Redis has no Vault dynamic secrets engine).
gwarek_redis_creds_k8s_name = "gwarek-redis-creds"  # pragma: allowlist secret
gwarek_redis_creds = core.v1.Secret(
    f"gwarek-redis-creds-{stack_info.env_suffix}",
    metadata=meta.v1.ObjectMetaArgs(
        name=gwarek_redis_creds_k8s_name,
        namespace=gwarek_namespace,
        labels=application_labels,
    ),
    string_data=gwarek_redis.address.apply(
        lambda address: {
            "REDIS_URL": f"rediss://default:{redis_config.require('password')}@{address}:6379",
        }
    ),
    opts=ResourceOptions(depends_on=[gwarek_redis], delete_before_replace=True),
)

# ---------------------------------------------------------------------------
# TLS cert + Keycloak OIDC via APISIX
# ---------------------------------------------------------------------------
cert_manager_certificate = OLCertManagerCert(
    f"gwarek-cert-manager-certificate-{stack_info.env_suffix}",
    cert_config=OLCertManagerCertConfig(
        application_name="gwarek",
        k8s_namespace=gwarek_namespace,
        k8s_labels=application_labels,
        create_apisixtls_resource=True,
        dest_secret_name="gwarek-tls",  # noqa: S106  # pragma: allowlist secret
        dns_names=[gwarek_domain],
    ),
)

gwarek_oidc_resources = OLApisixOIDCResources(
    f"gwarek-oidc-resources-{stack_info.env_suffix}",
    oidc_config=OLApisixOIDCConfig(
        application_name="gwarek",
        k8s_labels=application_labels,
        k8s_namespace=gwarek_namespace,
        oidc_logout_path="/logout/oidc",
        oidc_post_logout_redirect_uri=f"https://{gwarek_domain}/",
        oidc_session_absolute_timeout=60 * 20160,  # 2 weeks
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        oidc_use_session_secret=True,
        oidc_scope="openid profile email",
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/gwarek",
        vaultauth=gwarek_vaultauth,
    ),
)

# ---------------------------------------------------------------------------
# Container images
# ---------------------------------------------------------------------------
gwarek_api_image = cached_image_uri(
    format_docker_image_ref("mitodl/gwarek-api", "GWAREK")
)
gwarek_web_image = cached_image_uri(
    format_docker_image_ref("mitodl/gwarek-web", "GWAREK")
)

# Common env vars shared by the api and worker containers (both run from the
# same image). Secrets (DATABASE_URL, REDIS_URL, SECRET_KEY, and
# ANTHROPIC_API_KEY if set) come in via env_from, not listed here --
# unused here since LLM_BACKEND=bedrock below.
gwarek_common_env = [
    core.v1.EnvVarArgs(name="APP_ENV", value="production"),
    core.v1.EnvVarArgs(name="AUTH_ENABLED", value="true"),
    core.v1.EnvVarArgs(name="DEFAULT_ORG_ID", value="gwarek"),
    core.v1.EnvVarArgs(name="DEFAULT_ORG_NAME", value="MIT Open Learning"),
    core.v1.EnvVarArgs(name="KMS_BACKEND", value="local"),
    # IAM/IRSA auth via the bedrock:InvokeModel* grant above -- no static
    # ANTHROPIC_API_KEY needed in production.
    core.v1.EnvVarArgs(name="LLM_BACKEND", value="bedrock"),
    core.v1.EnvVarArgs(name="BEDROCK_REGION", value=aws_config.region),
    core.v1.EnvVarArgs(name="BEDROCK_MODEL_ID", value="us.anthropic.claude-sonnet-5"),
    core.v1.EnvVarArgs(name="STORAGE_BACKEND", value="s3"),
    core.v1.EnvVarArgs(name="STORAGE_BUCKET", value=gwarek_storage_bucket_name),
    core.v1.EnvVarArgs(name="STORAGE_PREFIX", value="analyzed/"),
    core.v1.EnvVarArgs(name="SNAPSHOT_S3_BUCKET", value=gwarek_snapshot_bucket_name),
    core.v1.EnvVarArgs(name="WIKI_S3_BUCKET", value=gwarek_wiki_bucket_name),
]
# DATABASE_URL is intentionally not sourced here -- see _db_env() above --
# since it must come in via named `env:` entries, not `envFrom:`, for
# Kubernetes' $(VAR) substitution to work.
gwarek_common_env_from = [
    core.v1.EnvFromSourceArgs(
        secret_ref=core.v1.SecretEnvSourceArgs(name=gwarek_redis_creds_k8s_name)
    ),
    core.v1.EnvFromSourceArgs(
        secret_ref=core.v1.SecretEnvSourceArgs(name=gwarek_app_secrets_k8s_name)
    ),
]

gwarek_replicas = gwarek_config.get_int("replicas") or 1

# ---------------------------------------------------------------------------
# api Deployment + Service
# ---------------------------------------------------------------------------
gwarek_api_deployment = apps_v1.Deployment(
    f"gwarek-api-deployment-{stack_info.env_suffix}",
    metadata=meta.v1.ObjectMetaArgs(
        name="gwarek-api",
        namespace=gwarek_namespace,
        labels=application_labels,
    ),
    spec=apps_v1.DeploymentSpecArgs(
        replicas=gwarek_replicas,
        selector=meta.v1.LabelSelectorArgs(
            match_labels={"app": "gwarek", "component": "api"}
        ),
        template=core.v1.PodTemplateSpecArgs(
            metadata=meta.v1.ObjectMetaArgs(
                labels={
                    **application_labels,
                    "component": "api",
                    **POD_SECURITY_GROUP_LABEL,
                },
            ),
            spec=core.v1.PodSpecArgs(
                service_account_name=gwarek_irsa_service_account_name,
                containers=[
                    core.v1.ContainerArgs(
                        name="api",
                        image=gwarek_api_image,
                        ports=[
                            core.v1.ContainerPortArgs(container_port=8000, name="http")
                        ],
                        env=[
                            *gwarek_common_env,
                            *_db_env(gwarek_db_app_secret_k8s_name),
                        ],
                        env_from=gwarek_common_env_from,
                        resources=core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "100m", "memory": "256Mi"},
                            limits={"memory": "512Mi"},
                        ),
                        liveness_probe=core.v1.ProbeArgs(
                            http_get=core.v1.HTTPGetActionArgs(
                                path="/health", port=8000
                            ),
                            initial_delay_seconds=15,
                            period_seconds=10,
                        ),
                        readiness_probe=core.v1.ProbeArgs(
                            http_get=core.v1.HTTPGetActionArgs(
                                path="/health", port=8000
                            ),
                            initial_delay_seconds=5,
                            period_seconds=5,
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        depends_on=[gwarek_db_app_secret, gwarek_app_secrets, gwarek_redis_creds]
    ),
)

gwarek_api_service = core.v1.Service(
    f"gwarek-api-service-{stack_info.env_suffix}",
    metadata=meta.v1.ObjectMetaArgs(
        name="gwarek-api",
        namespace=gwarek_namespace,
        labels=application_labels,
    ),
    spec=core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": "gwarek", "component": "api"},
        ports=[core.v1.ServicePortArgs(port=8000, target_port=8000, name="http")],
    ),
)

# ---------------------------------------------------------------------------
# worker Deployment (same image as api, different command — no Service, no
# HTTP port)
# ---------------------------------------------------------------------------
gwarek_worker_deployment = apps_v1.Deployment(
    f"gwarek-worker-deployment-{stack_info.env_suffix}",
    metadata=meta.v1.ObjectMetaArgs(
        name="gwarek-worker",
        namespace=gwarek_namespace,
        labels=application_labels,
    ),
    spec=apps_v1.DeploymentSpecArgs(
        replicas=gwarek_replicas,
        selector=meta.v1.LabelSelectorArgs(
            match_labels={"app": "gwarek", "component": "worker"}
        ),
        template=core.v1.PodTemplateSpecArgs(
            metadata=meta.v1.ObjectMetaArgs(
                labels={
                    **application_labels,
                    "component": "worker",
                    **POD_SECURITY_GROUP_LABEL,
                },
            ),
            spec=core.v1.PodSpecArgs(
                service_account_name=gwarek_irsa_service_account_name,
                containers=[
                    core.v1.ContainerArgs(
                        name="worker",
                        image=gwarek_api_image,
                        # Invoke arq as a module via the venv's python binary
                        # directly, not its console-script entrypoint — see
                        # the equivalent comment in api/Dockerfile's CMD.
                        command=[
                            "/app/.venv/bin/python",
                            "-m",
                            "arq",
                            "src.jobs.worker.WorkerSettings",
                        ],
                        env=[
                            *gwarek_common_env,
                            *_db_env(gwarek_db_app_secret_k8s_name),
                        ],
                        env_from=gwarek_common_env_from,
                        resources=core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "100m", "memory": "256Mi"},
                            limits={"memory": "512Mi"},
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        depends_on=[gwarek_db_app_secret, gwarek_app_secrets, gwarek_redis_creds]
    ),
)

# ---------------------------------------------------------------------------
# web Deployment + Service
# ---------------------------------------------------------------------------
gwarek_web_deployment = apps_v1.Deployment(
    f"gwarek-web-deployment-{stack_info.env_suffix}",
    metadata=meta.v1.ObjectMetaArgs(
        name="gwarek-web",
        namespace=gwarek_namespace,
        labels=application_labels,
    ),
    spec=apps_v1.DeploymentSpecArgs(
        replicas=gwarek_replicas,
        selector=meta.v1.LabelSelectorArgs(
            match_labels={"app": "gwarek", "component": "web"}
        ),
        template=core.v1.PodTemplateSpecArgs(
            metadata=meta.v1.ObjectMetaArgs(
                labels={**application_labels, "component": "web"},
            ),
            spec=core.v1.PodSpecArgs(
                containers=[
                    core.v1.ContainerArgs(
                        name="web",
                        image=gwarek_web_image,
                        ports=[
                            core.v1.ContainerPortArgs(container_port=3000, name="http")
                        ],
                        env=[
                            core.v1.EnvVarArgs(name="NODE_ENV", value="production"),
                            # Client-side fetches use relative same-origin
                            # paths in production (see web/src/lib/api.ts) —
                            # no NEXT_PUBLIC_API_URL needed. Server-side
                            # (Next.js) fetches call the api Service directly
                            # over the cluster network.
                            core.v1.EnvVarArgs(
                                name="INTERNAL_API_URL",
                                value="http://gwarek-api:8000",
                            ),
                        ],
                        resources=core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "50m", "memory": "256Mi"},
                            limits={"memory": "512Mi"},
                        ),
                        liveness_probe=core.v1.ProbeArgs(
                            http_get=core.v1.HTTPGetActionArgs(
                                path="/health", port=3000
                            ),
                            initial_delay_seconds=15,
                            period_seconds=10,
                        ),
                        readiness_probe=core.v1.ProbeArgs(
                            http_get=core.v1.HTTPGetActionArgs(
                                path="/health", port=3000
                            ),
                            initial_delay_seconds=5,
                            period_seconds=5,
                        ),
                    ),
                ],
            ),
        ),
    ),
)

gwarek_web_service = core.v1.Service(
    f"gwarek-web-service-{stack_info.env_suffix}",
    metadata=meta.v1.ObjectMetaArgs(
        name="gwarek-web",
        namespace=gwarek_namespace,
        labels=application_labels,
    ),
    spec=core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": "gwarek", "component": "web"},
        ports=[core.v1.ServicePortArgs(port=3000, target_port=3000, name="http")],
    ),
)

# ---------------------------------------------------------------------------
# One-off migration Job — runs `alembic upgrade head` against the admin
# Vault DB role before the api/worker Deployments are expected to serve
# traffic. Pulumi does not block on Job completion, so this ordering is
# advisory only (via depends_on on the Job's own secret) — confirm the
# migration actually completed before considering a deploy done (see the
# plan's Phase 6/7 manual verification step).
# ---------------------------------------------------------------------------
gwarek_migration_job = batch.v1.Job(
    f"gwarek-migration-job-{stack_info.env_suffix}",
    metadata=meta.v1.ObjectMetaArgs(
        name="gwarek-migrate",
        namespace=gwarek_namespace,
        labels=application_labels,
    ),
    spec=batch.v1.JobSpecArgs(
        backoff_limit=2,
        template=core.v1.PodTemplateSpecArgs(
            metadata=meta.v1.ObjectMetaArgs(labels=application_labels),
            spec=core.v1.PodSpecArgs(
                restart_policy="Never",
                containers=[
                    core.v1.ContainerArgs(
                        name="migrate",
                        image=gwarek_api_image,
                        command=[
                            "/app/.venv/bin/python",
                            "-m",
                            "alembic",
                            "upgrade",
                            "head",
                        ],
                        env=_db_env(gwarek_db_admin_secret_k8s_name),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(depends_on=[gwarek_db_admin_secret]),
)

# ---------------------------------------------------------------------------
# APISIX route — Keycloak OIDC in front of everything on gwarek_domain
# ---------------------------------------------------------------------------
gwarek_oidc_plugin = OLApisixPluginConfig(
    **gwarek_oidc_resources.get_full_oidc_plugin_config(unauth_action="auth")
)

gwarek_apisix_route = OLApisixRoute(
    name=f"gwarek-apisixroute-{stack_info.env_suffix}",
    k8s_namespace=gwarek_namespace,
    k8s_labels=application_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="api",
            priority=10,
            plugins=[gwarek_oidc_plugin],
            hosts=[gwarek_domain],
            paths=["/api/*", "/health"],
            backend_service_name="gwarek-api",
            backend_service_port=8000,
        ),
        OLApisixRouteConfig(
            route_name="web",
            priority=0,
            plugins=[gwarek_oidc_plugin],
            hosts=[gwarek_domain],
            paths=["/*"],
            backend_service_name="gwarek-web",
            backend_service_port=3000,
        ),
    ],
)
