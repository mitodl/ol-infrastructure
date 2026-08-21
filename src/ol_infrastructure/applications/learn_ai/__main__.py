# ruff: noqa: E501
"""Learn AI application infrastructure deployment (Pulumi)."""

import base64
import json
import mimetypes
import textwrap
from pathlib import Path

import pulumi_fastly as fastly
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    Output,
    ResourceOptions,
    export,
)
from pulumi_aws import ec2, get_caller_identity, iam, route53, s3

from bridge.lib.constants import (
    FASTLY_A_TLS_1_3,
    apisix_oidc_session_cookie_name,
    mit_learn_session_cookie_name,
)
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_REDIS_PORT,
    DEFAULT_WSGI_PORT,
    ONE_MEGABYTE_BYTE,
    STATIC_ASSET_MAX_AGE_SECONDS,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services import appdb
from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
    stale_session_cookie_cleanup_plugin,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    GranianConfig,
    OLApplicationK8s,
    OLApplicationK8sCeleryBeatConfig,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
from ol_infrastructure.lib.k8s_keda import (
    build_webapp_keda_config,
    create_webapp_prometheus_trigger_auth,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    KubernetesServiceAppProtocol,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    docker_image_config_kwargs,
    format_docker_image_ref,
    make_stack_reference,
    merge_otel_resource_attributes,
    parse_stack,
)
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

aws_account = get_caller_identity()
stack_info = parse_stack()
env_name = f"learn_ai-{stack_info.env_suffix}"

cluster_stack = make_stack_reference(projects.EKS, f"applications.{stack_info.name}")
cluster_substructure_stack = make_stack_reference(
    projects.EKS_SUB, f"applications.{stack_info.name}"
)
dns_stack = make_stack_reference(projects.DNS, "default")
monitoring_stack = make_stack_reference(projects.MONITORING, "default")
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
opik_stack = make_stack_reference(projects.OPIK, stack_info.name)
policy_stack = make_stack_reference(projects.POLICIES, "default")
sentry_stack = make_stack_reference(projects.SENTRY, "default")
vault_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
)
vector_log_proxy_stack = make_stack_reference(
    projects.VECTOR_LOG_PROXY, f"operations.{stack_info.name}"
)

apps_vpc = network_stack.require_output("applications_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]
learn_ai_environment = f"applications-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": learn_ai_environment},
)
learn_ai_config = Config("learn_ai")
vault_config = Config("vault")

slack_channel = learn_ai_config.get("slack_channel")  # Optional Slack channel
apisix_ingress_class = learn_ai_config.get("apisix_ingress_class") or "apisix"

setup_vault_provider(stack_info)
fastly_provider = get_fastly_provider()

k8s_global_labels = K8sGlobalLabels(
    ou=BusinessUnit.mit_learn, service=Services.mit_learn, stack=stack_info
).model_dump()
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

learn_ai_namespace = "learn-ai"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(learn_ai_namespace, ns)
)

ol_zone_id = dns_stack.require_output("ol")["id"]

################################################
# Frontend storage bucket
learn_ai_app_storage_bucket_name = f"ol-mit-learn-ai-{stack_info.env_suffix}"

learn_ai_app_storage_bucket_config = S3BucketConfig(
    bucket_name=learn_ai_app_storage_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    intelligent_tiering_archive_access_days=None,  # Fastly backend
    intelligent_tiering_deep_archive_access_days=None,
    tags=aws_config.tags,
)

learn_ai_app_storage_bucket = OLBucket(
    f"learn-ai-app-storage-bucket-{stack_info.env_suffix}",
    config=learn_ai_app_storage_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"learn-ai-app-storage-bucket-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name=f"learn-ai-app-storage-bucket-versioning-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name=f"learn-ai-app-storage-bucket-ownership-controls-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name=f"learn-ai-app-storage-bucket-public-access-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

learn_ai_app_storage_bucket_policy = s3.BucketPolicy(
    f"learn-ai-app-storage-bucket-policy-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket.bucket_v2.id,
    policy=learn_ai_app_storage_bucket.bucket_v2.arn.apply(
        lambda arn: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "PublicRead",
                        "Effect": "Allow",
                        "Principal": "*",
                        "Action": "s3:GetObject",
                        "Resource": f"{arn}/*",
                    }
                ],
            }
        )
    ),
)

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3.putobjectacl"]}],
    },
    "RESOURCE_EFFECTIVLY_STAR": {},
    "RESOURCE_MISMATCH": {},
}

##################################
#     General K8S + IAM Config   #
##################################

learn_ai_bedrock_policy_document = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "InvokeDomainInferenceProfiles",
            "Effect": "Allow",
            "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
            "Resource": [
                "arn:aws:bedrock:*:*:inference-profile/*",
                "arn:aws:bedrock:*:*:foundation-model/*",
            ],
        }
    ],
}

learn_ai_bedrock_policy = iam.Policy(
    f"learn-ai-bedrock-policy-{stack_info.env_suffix}",
    name=f"learn-ai-trustrole-bedrock-iam-policy-{stack_info.env_suffix}",
    policy=lint_iam_policy(
        learn_ai_bedrock_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
)


learn_ai_service_account_name = "learn-ai-admin"
learn_ai_trust_role_config = OLEKSTrustRoleConfig(
    account_id=aws_account.account_id,
    cluster_name=f"data-{stack_info.name}",
    cluster_identities=cluster_stack.require_output("cluster_identities"),
    description="Trust role for allowing the learn_ai service account to "
    "access the aws API",
    policy_operator="StringEquals",
    role_name="learn_ai",
    service_account_identifier=f"system:serviceaccount:{learn_ai_namespace}:{learn_ai_service_account_name}",
    tags=aws_config.tags,
)

learn_ai_trust_role = OLEKSTrustRole(
    f"learn-ai-ol-trust-role-{stack_info.env_suffix}",
    role_config=learn_ai_trust_role_config,
)
iam.RolePolicyAttachment(
    "learn-ai-bedrock-policy-attachement-{stack_info.env_suffix}",
    policy_arn=learn_ai_bedrock_policy.arn,
    role=learn_ai_trust_role.role.name,
)

learn_ai_service_account = kubernetes.core.v1.ServiceAccount(
    "learn-ai-service-account-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=learn_ai_service_account_name,
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
        annotations={
            "eks.amazonaws.com/role-arn": learn_ai_trust_role.role.arn,
        },
    ),
    automount_service_account_token=False,
)


################################################
# Fastly configuration
vector_log_proxy_secrets = read_yaml_secrets(
    Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
)
fastly_proxy_credentials = vector_log_proxy_secrets["fastly"]
encoded_fastly_proxy_credentials = base64.b64encode(
    f"{fastly_proxy_credentials['username']}:{fastly_proxy_credentials['password']}".encode()
).decode("utf8")
vector_log_proxy_domain = vector_log_proxy_stack.require_output(
    "vector_log_proxy_domain"
)

learn_ai_frontend_domain = learn_ai_config.require("frontend_domain")
fastly_access_logging_bucket = monitoring_stack.require_output(
    "fastly_access_logging_bucket"
)
fastly_access_logging_iam_role = monitoring_stack.require_output(
    "fastly_access_logging_iam_role"
)
gzip_settings: dict[str, set[str]] = {"extensions": set(), "content_types": set()}
for k, v in mimetypes.types_map.items():
    if k in (
        ".json",
        ".pdf",
        ".jpeg",
        ".jpg",
        ".html.css",
        ".js",
        ".svg",
        ".png",
        ".gif",
        ".xml",
        ".vtt",
        ".srt",
    ):
        gzip_settings["extensions"].add(k.strip("."))
        gzip_settings["content_types"].add(v)
learn_ai_fastly_service = fastly.ServiceVcl(
    f"learn-ai-fastly-service-{stack_info.env_suffix}",
    name=f"Learn AI {stack_info.env_suffix}",
    comment="Managed by Pulumi",
    backends=[
        fastly.ServiceVclBackendArgs(
            address=learn_ai_app_storage_bucket.bucket_v2.bucket_domain_name,
            name="learn-ai",
            override_host=learn_ai_app_storage_bucket.bucket_v2.bucket_domain_name,
            port=DEFAULT_HTTPS_PORT,
            ssl_cert_hostname=learn_ai_app_storage_bucket.bucket_v2.bucket_domain_name,
            ssl_sni_hostname=learn_ai_app_storage_bucket.bucket_v2.bucket_domain_name,
            use_ssl=True,
        ),
    ],
    gzips=[
        fastly.ServiceVclGzipArgs(
            name="enable-gzip-compression",
            extensions=list(gzip_settings["extensions"]),
            content_types=list(gzip_settings["content_types"]),
        )
    ],
    product_enablement=fastly.ServiceVclProductEnablementArgs(
        brotli_compression=True,
    ),
    cache_settings=[],
    conditions=[],
    dictionaries=[],
    domains=[
        fastly.ServiceVclDomainArgs(
            comment=f"learn_ai {stack_info.env_suffix} Application",
            name=learn_ai_frontend_domain,
        ),
    ],
    request_settings=[
        fastly.ServiceVclRequestSettingArgs(
            force_ssl=True,
            name="Generated by force TLS and enable HSTS",
            xff="leave",
        )
    ],
    headers=[
        fastly.ServiceVclHeaderArgs(
            action="set",
            destination="http.Strict-Transport-Security",
            name="Generated by force TLS and enable HSTS",
            source='"max-age=300"',
            type="response",
        ),
    ],
    snippets=[
        fastly.ServiceVclSnippetArgs(
            name="Add frontend to path",
            content=Path("files/frontend_path_prefix.vcl").read_text(),
            type="recv",
        ),
        fastly.ServiceVclSnippetArgs(
            name="Return custom 404 page",
            content=Path("files/custom_404.vcl").read_text(),
            type="deliver",
        ),
        fastly.ServiceVclSnippetArgs(
            name="Redirect for to correct domain",
            content=textwrap.dedent(
                rf"""
                # redirect to the correct host/domain
                if (obj.status == 618 && obj.response == "redirect-host") {{
                  set obj.status = 302;
                  set obj.http.Location = "https://" + "{learn_ai_config.require("frontend_domain")}" + req.url.path + if (std.strlen(req.url.qs) > 0, "?" req.url.qs, "");
                  return (deliver);
                }}
                """
            ),
            type="error",
        ),
    ],
    logging_https=[
        fastly.ServiceVclLoggingHttpArgs(
            url=Output.all(domain=vector_log_proxy_domain).apply(
                lambda kwargs: f"https://{kwargs['domain']}/fastly"
            ),
            name=f"fastly-learn_ai-{stack_info.env_suffix}-https-logging-args",
            content_type="application/json",
            format=build_fastly_log_format_string(additional_static_fields={}),
            format_version=2,
            header_name="Authorization",
            header_value=f"Basic {encoded_fastly_proxy_credentials}",
            json_format="0",
            method="POST",
            request_max_bytes=ONE_MEGABYTE_BYTE,
        )
    ],
    opts=ResourceOptions.merge(fastly_provider, ResourceOptions()),
)

# Point the frontend domain at fastly
five_minutes = 60 * 5
route53.Record(
    f"learn-ai-frontend-dns-{stack_info.env_suffix}",
    name=learn_ai_config.require("frontend_domain"),
    allow_overwrite=True,
    type="A",
    ttl=five_minutes,
    records=[str(addr) for addr in FASTLY_A_TLS_1_3],
    zone_id=ol_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)

################################################
# Put the application secrets into vault
learn_ai_vault_secrets = read_yaml_secrets(
    Path(f"learn_ai/secrets.{stack_info.env_suffix}.yaml"),
)
if stack_info.env_suffix != "ci":
    mitlearn_posthog_secrets = read_yaml_secrets(
        Path(f"mitopen/secrets.{stack_info.env_suffix}.yaml")
    )["posthog"]
    learn_ai_vault_secrets.update(
        {
            "POSTHOG_PROJECT_API_KEY": mitlearn_posthog_secrets["project_api_key"],
            "POSTHOG_PERSONAL_API_KEY": mitlearn_posthog_secrets["personal_api_key"],
        }
    )
learn_ai_vault_mount = vault.Mount(
    f"learn-ai-secrets-mount-{stack_info.env_suffix}",
    path="secret-learn-ai",
    type="kv-v2",
    options={"version": "2"},
    description="Secrets for the learn ai application.",
    opts=ResourceOptions(delete_before_replace=True),
)
learn_ai_static_vault_secrets = vault.generic.Secret(
    f"learn-ai-secrets-{stack_info.env_suffix}",
    path=learn_ai_vault_mount.path.apply("{}/secrets".format),
    data_json=sentry_stack.require_output("learn_ai_sentry_dsn").apply(
        lambda dsn: json.dumps({**learn_ai_vault_secrets, "SENTRY_DSN": dsn})
    ),
)

################################################
# Application security group
# Needs to happen ebfore the database security group is created
learn_ai_application_security_group = ec2.SecurityGroup(
    f"learn-ai-application-security-group-{stack_info.env_suffix}",
    name=f"learn-ai-application-security-group-{stack_info.env_suffix}",
    description="Access control for the learn-ai application pods.",
    # allow all egress traffic
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(
        k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs,
    ),
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

################################################
# RDS configuration and networking setup

learn_ai_db_config = appdb.OLAppDatabaseConfig(
    app_name="learn-ai",
    app_security_group=learn_ai_application_security_group,
    app_db_name="learnai",
    aws_config=aws_config,
    app_vpc=apps_vpc,
    app_db_password=learn_ai_config.get("db_password"),
    alias_map={
        appdb.AliasKey.secgroup: [Alias(parent=ROOT_STACK_RESOURCE)],
        appdb.AliasKey.db: [Alias(parent=ROOT_STACK_RESOURCE)],
    },
)
learn_ai_db = appdb.OLAppDatabase(learn_ai_db_config)

# Redis Cluster configuration and networking setup
redis_config = Config("redis")
redis_defaults = defaults(stack_info)["redis"]
instance_type = redis_config.get("instance_type") or redis_defaults["instance_type"]
redis_defaults["instance_type"] = instance_type
redis_cluster_security_group = ec2.SecurityGroup(
    f"learn-ai-redis-cluster-security-group-{stack_info.env_suffix}",
    name_prefix=f"learn-ai-redis-security-group-{stack_info.env_suffix}",
    description="Access control for the learn-ai redis cluster.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                learn_ai_application_security_group.id,
                cluster_substructure_stack.require_output(
                    "cluster_keda_security_group_id"
                ),
            ],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description="Allow application pods to talk to Redis",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=operations_vpc["k8s_pod_subnet_cidrs"],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description="Allow Operations VPC celery monitoring pods to talk to Redis",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_config.require("password"),
    cluster_mode_enabled=False,
    encrypted=True,
    engine_version="7.2",
    engine="valkey",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for learn UI tasks and caching.",
    cluster_name=f"learn-ai-redis-{stack_info.env_suffix}",
    subnet_group=apps_vpc["elasticache_subnet"],
    security_groups=[redis_cluster_security_group.id],
    tags=aws_config.tags,
    **redis_defaults,
)
redis_cache = OLAmazonCache(
    redis_cache_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"learn-ai-redis-{stack_info.env_suffix}-redis-elasticache-cluster"
            )
        ]
    ),
)

################################################
# Create vault policy and associate it with an auth backend role
# on the vault k8s cluster auth endpoint
learn_ai_vault_policy = vault.Policy(
    f"learn-ai-vault-policy-{stack_info.env_suffix}",
    name="learn-ai",
    policy=Path(__file__).parent.joinpath("learn_ai_policy.hcl").read_text(),
)

learn_ai_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    f"learn-ai-vault-auth-backend-role-{stack_info.env_suffix}",
    role_name="learn-ai",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[learn_ai_namespace],
    token_policies=[learn_ai_vault_policy.name],
)

vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="learn-ai",
    namespace=learn_ai_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=learn_ai_vault_auth_backend_role.role_name,
)

vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_vault_auth_backend_role],
    ),
)

# Load the database creds into a k8s secret via VSO
db_creds_secret_name = "pgsql-db-creds"  # noqa: S105  # pragma: allowlist secret
db_creds_secret = Output.all(
    address=learn_ai_db.app_db.db_instance.address,
    port=learn_ai_db.app_db.db_instance.port,
    db_name=learn_ai_db.app_db.db_instance.db_name,
).apply(
    lambda db: OLVaultK8SSecret(
        f"learn-ai-{stack_info.env_suffix}-db-creds-secret",
        OLVaultK8SDynamicSecretConfig(
            name="learn-ai-db-creds",
            namespace=learn_ai_namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=db_creds_secret_name,
            labels=k8s_global_labels,
            mount=learn_ai_db.app_db_vault_backend.db_mount.path,
            path="creds/app",
            restart_target_kind="Deployment",
            restart_target_name="learn-ai-app",
            templates={
                "DATABASE_URL": f'postgres://{{{{ get .Secrets "username"}}}}:{{{{ get .Secrets "password" }}}}@{db["address"]}:{db["port"]}/{db["db_name"]}',
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            parent=vault_k8s_resources,
            depends_on=[learn_ai_db],
        ),
    )
)

# Load the redis creds into a normal k8s secret
redis_creds_secret_name = "redis-creds"  # noqa: S105  # pragma: allowlist secret
redis_creds = kubernetes.core.v1.Secret(
    f"learn-ai-{stack_info.env_suffix}-redis-creds",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=redis_creds_secret_name,
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
    ),
    string_data=redis_cache.address.apply(
        lambda address: {
            # Duplicate the Redis domain to make healthchecks happy
            "REDIS_URL": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/0",
            "REDIS_DOMAIN": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/0",
            "REDIS_SSL_CERT_REQS": "required",
            "CELERY_BROKER_URL": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/1?ssl_cert_reqs=required",
            "CELERY_RESULT_BACKEND": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/1?ssl_cert_reqs=required",
        }
    ),
    opts=ResourceOptions(
        depends_on=[redis_cache],
        delete_before_replace=True,
    ),
)

# Load the static secrets into a k8s secret via VSO
static_secrets_name = "learn-ai-static-secrets"  # pragma: allowlist secret
static_secrets = OLVaultK8SSecret(
    name=f"learn-ai-{stack_info.env_suffix}-static-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="learn-ai-static-secrets",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
        dest_secret_name=static_secrets_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-learn-ai",
        mount_type="kv-v2",
        path="secrets",
        includes=["*"],
        excludes=[],
        exclude_raw=True,
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
        depends_on=[learn_ai_static_vault_secrets],
    ),
)

# Opik LLM-observability credentials. The ``ol-opik-client`` Keycloak client
# (client-credentials grant) and its realm discovery URL are published by the
# keycloak substructure to Vault at ``secret-operations/sso/opik``. Sync them
# into a K8s secret via VSO and expose them to the app as the OPIK_KEYCLOAK_*
# env vars. The token endpoint is derived from the realm ``url`` key so it stays
# correct across environments (sso-ci / sso-qa / sso) without hardcoding.
opik_keycloak_secret_name = (
    "learn-ai-opik-keycloak"  # pragma: allowlist secret  # noqa: S105
)
opik_keycloak_secret = OLVaultK8SSecret(
    name=f"learn-ai-{stack_info.env_suffix}-opik-keycloak-secret",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="learn-ai-opik-keycloak",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
        dest_secret_name=opik_keycloak_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/opik",
        restart_targets=[{"kind": "Deployment", "name": "learn-ai-app"}],
        templates={
            "OPIK_KEYCLOAK_CLIENT_ID": '{{ get .Secrets "client_id" }}',
            "OPIK_KEYCLOAK_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            "OPIK_KEYCLOAK_TOKEN_URL": (
                '{{ get .Secrets "url" }}/protocol/openid-connect/token'
            ),
        },
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
    ),
)

# Dynamic Azure AD credentials for Azure OpenAI. Vault mints a service principal
# scoped to learn-ai's own Cognitive Services account; the restart target matches the
# db-creds secret above so a rotation actually reaches the running pods. Additive --
# the existing OPENAI_API_KEY wiring and AI_DEFAULT_*_MODEL values are untouched.
#
# Gated on learn_ai:azure_openai_tenant_id so this is a no-op until the
# substructure/vault/azure mount exists in the environment: reading a role from a
# mount Vault does not have fails the VaultDynamicSecret.
azure_openai_tenant_id = learn_ai_config.get("azure_openai_tenant_id")
azure_openai_secret_name = (
    "learn-ai-azure-openai-creds"  # pragma: allowlist secret  # noqa: S105
)
if azure_openai_tenant_id:
    azure_openai_secret = OLVaultK8SSecret(
        f"learn-ai-{stack_info.env_suffix}-azure-openai-secret",
        OLVaultK8SDynamicSecretConfig(
            name="learn-ai-azure-openai-creds",
            namespace=learn_ai_namespace,
            labels=k8s_global_labels,
            dest_secret_name=azure_openai_secret_name,
            dest_secret_labels=k8s_global_labels,
            mount="azure-openai",
            path="creds/ol-learn-ai-openai",
            # Every workload below gets this secret through the component-wide
            # env_from_secret_names, so all of them have to restart on rotation,
            # not just the webapp. Names match what OLApplicationK8s generates:
            # "{app}-app" for the webapp, "{app}-{worker_name}-celery-worker"
            # per worker (underscores become dashes), "{app}-celery-beat".
            restart_targets=[
                OLVaultRestartTarget(kind="Deployment", name=name)
                for name in (
                    "learn-ai-app",
                    "learn-ai-default-celery-worker",
                    "learn-ai-edx-content-celery-worker",
                    "learn-ai-celery-beat",
                )
            ],
            templates={
                "AZURE_OPENAI_CLIENT_ID": '{{ get .Secrets "client_id" }}',
                "AZURE_OPENAI_CLIENT_SECRET": '{{ get .Secrets "client_secret" }}',
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            parent=vault_k8s_resources,
        ),
    )

env_vars = dict(learn_ai_config.require_object("env_vars") or {})

# Non-secret half of the Azure OpenAI wiring. The endpoint is derived rather than read
# from the azure stack: infrastructure/azure/openai sets each account's custom
# subdomain to its own name, so the URL follows from the environment. The tenant is
# the one value that cannot be derived, which is why it doubles as the enable switch.
if azure_openai_tenant_id:
    env_vars.update(
        {
            "AZURE_OPENAI_ENDPOINT": (
                f"https://ol-openai-learn-ai-{stack_info.env_suffix}.openai.azure.com/"
            ),
            "AZURE_OPENAI_TENANT_ID": azure_openai_tenant_id,
            "AZURE_OPENAI_API_VERSION": (
                learn_ai_config.get("azure_openai_api_version") or "2024-10-21"
            ),
        }
    )

# Opik instrumentation (non-secret settings). OPIK_URL_OVERRIDE is derived from
# the opik application stack's exported URL so it tracks the deployed instance
# per environment; the workspace/project are static for our OSS install. The
# Keycloak client id/secret and token URL arrive via envFrom (see the VSO secret
# above). Note: do NOT set OPIK_API_KEY — the app's Keycloak auth flow owns the
# Authorization header (see the opik stack's OPIK_SDK_KEYCLOAK_AUTH.md).
env_vars.update(
    {
        "OPIK_URL_OVERRIDE": opik_stack.require_output("opik_url").apply(
            lambda url: f"{url}/api/"
        ),
        "OPIK_WORKSPACE": "default",
        "OPIK_PROJECT_NAME": "learn-ai",
    }
)

# Unconditionally append k8s labels to OTEL_RESOURCE_ATTRIBUTES so all telemetry
# signals carry organizational metadata regardless of stack environment.
merge_otel_resource_attributes(env_vars, k8s_global_labels)

# Horizontal scaling is KEDA-driven on APISIX request rate and p95 latency, with a
# CPU trigger as a backstop, matching edxapp, mit-learn and mitxonline. CPU is an
# especially poor saturation signal for this app: it spends most of a request waiting
# on upstream LLM and embedding calls, so a saturated pod can sit near-idle on CPU
# while requests queue behind it.
#
# The route matcher covers both the direct and the mit-learn-fronted routes. Verified
# against live metrics -- `count by (route) (apisix_http_status)` returns
# learn-ai_learn-ai-production-https-olapisixroute_passauth and
# learn-ai_mit-learn-learn-ai-production-https-olapisixroute_{passauth,reqauth}, so the
# leading `.*` is load-bearing.
webapp_trigger_auth, webapp_trigger_auth_name = create_webapp_prometheus_trigger_auth(
    application_name="learn-ai",
    env_name=env_name,
    namespace=learn_ai_namespace,
    k8s_global_labels=k8s_global_labels,
    stack_info=stack_info,
    vault_k8s_resources=vault_k8s_resources,
)

learn_ai_webapp_keda_config = build_webapp_keda_config(
    trigger_auth_name=webapp_trigger_auth_name,
    route_matcher=f"learn-ai_.*learn-ai-{stack_info.env_suffix}-https-olapisixroute_.*",
    container_name="learn-ai-app",
    requests_threshold=learn_ai_config.get("autoscaling_requests_threshold") or "20",
    latency_threshold=learn_ai_config.get("autoscaling_latency_threshold") or "2000",
    cpu_threshold=learn_ai_config.get("autoscaling_cpu_threshold") or "60",
)

# Instantiate the OLApplicationK8s component
learn_ai_app_k8s = OLApplicationK8s(
    ol_app_k8s_config=OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=env_vars,
        application_name="learn-ai",
        application_namespace=learn_ai_namespace,
        application_lb_service_name="learn-ai-webapp",
        application_lb_service_port_name="http",
        application_lb_service_app_protocol=KubernetesServiceAppProtocol.WS,
        k8s_global_labels=k8s_global_labels,
        env_from_secret_names=[
            db_creds_secret_name,
            redis_creds_secret_name,
            static_secrets_name,
            opik_keycloak_secret_name,
            *([azure_openai_secret_name] if azure_openai_tenant_id else []),
        ],
        application_security_group_id=learn_ai_application_security_group.id,
        # Use the fixed name used in the SecurityGroupPolicy spec
        application_security_group_name=Output.from_input("learn-ai-app"),
        application_service_account_name=learn_ai_service_account.metadata.name,
        application_image_repository="mitodl/learn-ai-app",
        **docker_image_config_kwargs("LEARN_AI"),
        application_min_replicas=learn_ai_config.get("min_replicas") or 2,
        application_cmd_array=["uvicorn"],
        application_arg_array=[
            "main.asgi:application",
            "--reload",
            "--host",
            "0.0.0.0",  # noqa: S104
            "--port",
            f"{DEFAULT_WSGI_PORT}",
        ],
        granian_config=GranianConfig(
            interface="asgi",
            workers=1,
            runtime_mode=None,
            # Holding pin: the component default dropped to 1. asgi forces
            # blocking_threads=1 regardless, so this is the only axis on which the
            # overhaul touches learn_ai until its review task.
            # See docs/plans/granian-configuration-overhaul.md
            runtime_threads=2,
            no_ws=False,
            backlog=None,
            log_level="debug",
            application_module="main.asgi:application",
            nginx_config_filename="web.conf",  # learn_ai shares one nginx config for all server types
            enable_metrics=True,
            # Serve /static/* from Granian's Rust layer instead of the sidecar
            # (docs/plans/remove-nginx-sidecar.md, stage 4), matching the
            # STATIC_ROOT/STATIC_URL precedent from ocw_studio/xpro. Only
            # meaningful when the sidecar is actually dropped below, i.e. in
            # the use_granian branch.
            static_path_mounts=["/src/staticfiles"],
            static_path_expires=STATIC_ASSET_MAX_AGE_SECONDS,
        )
        if learn_ai_config.get_bool("use_granian")
        else None,
        slack_channel=slack_channel,
        vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
        # The sidecar is only redundant once Granian is actually serving the
        # app (static_path_mounts above); the use_granian=False branch still
        # runs bare uvicorn with no static handling of its own, so it keeps
        # the sidecar. See docs/plans/remove-nginx-sidecar.md.
        import_nginx_config=not learn_ai_config.get_bool("use_granian"),
        # Nginx resources (defaults from component are fine)
        # App container resources
        resource_requests={"cpu": "100m", "memory": "1000Mi"},
        resource_limits={"memory": "1000Mi"},
        init_migrations=True,
        init_collectstatic=True,  # Assuming createcachetable is not needed or handled elsewhere
        celery_worker_configs=[
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="default",
                redis_host=redis_cache.address,
                redis_database_index="1",
                redis_password=redis_config.require("password"),
                resource_requests={"cpu": "100m", "memory": "1000Mi"},
                resource_limits={"memory": "1000Mi"},
            ),
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="edx_content",
                redis_host=redis_cache.address,
                redis_database_index="1",
                redis_password=redis_config.require("password"),
                resource_requests={"cpu": "100m", "memory": "1000Mi"},
                resource_limits={"memory": "1000Mi"},
            ),
        ],
        celery_beat_config=OLApplicationK8sCeleryBeatConfig(
            scheduler="celery.beat.PersistentScheduler",
            resource_requests={"cpu": "10m", "memory": "384Mi"},
            resource_limits={"memory": "384Mi"},
        ),
        # hpa_scaling_metrics is left at the component default. It is unused here:
        # the component builds a KEDA ScaledObject instead of a native HPA when
        # webapp_keda_config is set, and the CPU backstop lives in the KEDA triggers.
        # Memory is managed vertically by the component's webapp memory VPA.
        webapp_keda_config=learn_ai_webapp_keda_config,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[
            learn_ai_db,
            db_creds_secret,
            redis_creds,
            static_secrets,
            opik_keycloak_secret,
            vault_k8s_resources,
            learn_ai_application_security_group,
            # The ScaledObject references the trigger authentication by name.
            webapp_trigger_auth,
        ],
    ),
)

# Reconstruct variables needed for Celery deployment
application_image_repository_and_tag = format_docker_image_ref(
    "mitodl/learn-ai-app", "LEARN_AI"
)

learn_ai_deployment_env_vars = []
for k, v in (learn_ai_config.require_object("env_vars") or {}).items():
    learn_ai_deployment_env_vars.append(
        kubernetes.core.v1.EnvVarArgs(
            name=k,
            value=v,
        )
    )

# Build a list of sensitive env vars for the deployment config via envFrom
learn_ai_deployment_envfrom = [
    # Database creds
    kubernetes.core.v1.EnvFromSourceArgs(
        secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
            name=db_creds_secret_name,
        ),
    ),
    # Redis Configuration
    kubernetes.core.v1.EnvFromSourceArgs(
        secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
            name=redis_creds_secret_name,
        ),
    ),
    # static secrets from secrets-learn-ai/secrets
    kubernetes.core.v1.EnvFromSourceArgs(
        secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
            name=static_secrets_name,
        ),
    ),
]

# Create the apisix custom resources since it doesn't support gateway-api yet

# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_plugin_config/
# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_pluginconfig_v2/

# Instantiate shared plugins component
learn_ai_shared_plugins = OLApisixSharedPlugins(
    f"learn-ai-{stack_info.env_suffix}-ol-shared-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="learn-ai",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=learn_ai_namespace,
        k8s_labels=k8s_global_labels,
        enable_defaults=True,
        plugins=[
            # Both of learn-ai's OIDC resources have now moved off
            # lua-resty-session's default "session" name, so every current user
            # has a dead one of those in their browser: on the legacy host from
            # this file's own rename, and on api.<env>.learn.mit.edu from the
            # rename in #5219.  Nothing else evicts them -- #5219 attached no
            # cleanup to learn-ai, and mit-learn's cleanup only fires on
            # responses from mit-learn's own routes, not from /ai/*.
            #
            # No cookie_domains: this config is referenced from both hosts, and a
            # Domain=.learn.mit.edu deletion emitted from api-learn-ai.ol.mit.edu
            # would just be rejected by the browser.  Host-only is also the only
            # scope learn-ai ever wrote a "session" cookie at, on either host.
            # Safe to delete once the old cookies have aged out of circulation.
            stale_session_cookie_cleanup_plugin(),
        ],
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

# Instantiate OIDC resources component for learn-ai's own legacy host.
#
# This one deliberately sets no cookie domain: the legacy host is not under
# learn.mit.edu, so a Domain=.learn.mit.edu cookie would be rejected there
# outright.  A host-only cookie is correct for it -- there is no mit-learn
# session on that host to share.  The routes served from mit-learn's own host
# use the separate resource below.
learn_ai_oidc_resources = OLApisixOIDCResources(
    f"learn-ai-{stack_info.env_suffix}-oidc-resources",
    oidc_config=OLApisixOIDCConfig(
        application_name="learn-ai",
        k8s_labels=k8s_global_labels,
        k8s_namespace=learn_ai_namespace,
        # Narrower than the component default, which adds organization:*.  Safe
        # on this host: its session is not shared with mit-learn, and learn-ai's
        # own APISIX_USERDATA_MAP reads no organization claim.
        oidc_scope="openid profile email",
        oidc_introspection_endpoint_auth_method="client_secret_basic",  # Default
        oidc_logout_path="/logout",
        oidc_post_logout_redirect_uri="/",
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        # Its own name, not the shared MIT Learn one: this host is not under
        # learn.mit.edu, so no mit-learn cookie is ever sent here and there is no
        # session to share -- naming it after mit-learn would only mislead
        # whoever next reads a Cookie header from this host.  The resource below
        # is the one that actually participates in the shared MIT Learn session.
        oidc_session_cookie_name=apisix_oidc_session_cookie_name(
            "learn-ai",
            stack_info.env_suffix,
        ),
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/mitlearn",  # Use mitlearn SSO config
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, parent=vault_k8s_resources),
)

learn_ai_api_domain = learn_ai_config.require("backend_domain")  # Legacy domain
learn_api_domain = learn_ai_config.require("learn_backend_domain")  # New domain

# Instantiate a second OIDC resource for the /ai/* routes served from mit-learn's
# own host (api.<env>.learn.mit.edu).
#
# Identical to the legacy-host resource above apart from the cookie domain,
# which is why it has to be a separate resource rather than one shared config:
# the domain lives on the plugin config, and a single config cannot be both
# host-only on api-learn-ai.ol.mit.edu and domain-scoped on
# api.<env>.learn.mit.edu.  ol_analytics_api/__main__.py splits its own
# .ol.mit.edu and .learn.mit.edu hosts into "<app>" and "<app>-learn" resources
# for the same reason.
#
# The domain is what matters here.  These routes' unauth_action="pass" plugins
# recognize the session mit-learn's login flow set, which requires the cookie
# name to match -- but the "reqauth" route below performs a real login
# (unauth_action="auth" on /ai/http/login/, which the learn-ai frontend uses as
# its log-in link).  Without a matching domain that login writes a *host-only*
# cookie on api.<env>.learn.mit.edu under the shared name: a second, separate
# entry in the browser's jar that shadows mit-learn's .learn.mit.edu cookie,
# since a cookie's identity includes whether it is host-only.  Both are then
# sent on every request and the gateway reads whichever comes first, so an OIDC
# callback can be handed a session envelope with no state for the flow in
# progress.  With the domain set, all of them read and write the one shared
# cookie.
learn_ai_mit_learn_oidc_resources = OLApisixOIDCResources(
    f"learn-ai-mit-learn-{stack_info.env_suffix}-oidc-resources",
    oidc_config=OLApisixOIDCConfig(
        application_name="learn-ai-mit-learn",
        k8s_labels=k8s_global_labels,
        k8s_namespace=learn_ai_namespace,
        # No oidc_scope override, unlike the legacy-host resource above: the
        # "reqauth" route below writes the shared session, so it has to request
        # the same claims mit-learn's own login does.  The component default
        # adds organization:*, which mit-learn maps to users.User.organizations
        # via APISIX_USERDATA_MAP and reads to decide whether to skip onboarding.
        oidc_introspection_endpoint_auth_method="client_secret_basic",  # Default
        oidc_logout_path="/logout",
        oidc_post_logout_redirect_uri="/",
        oidc_session_idling_timeout=0,
        oidc_session_rolling_timeout=0,
        oidc_session_cookie_domain=learn_api_domain.removeprefix("api"),
        # Must track mit_learn/__main__.py's oidc_session_cookie_name exactly.
        oidc_session_cookie_name=mit_learn_session_cookie_name(
            stack_info.env_suffix,
        ),
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/mitlearn",  # Use mitlearn SSO config
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, parent=vault_k8s_resources),
)

# ApisixUpstream resources don't seem to work but we don't really need them?
# Ref: https://github.com/apache/apisix-ingress-controller/issues/1655
# Ref: https://github.com/apache/apisix-ingress-controller/issues/1855

# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_route_v2/
# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/

# Define proxy-rewrite plugin once
proxy_rewrite_plugin = OLApisixPluginConfig(
    name="proxy-rewrite",
    enable=True,
    config={
        "regex_uri": [
            "/ai/(.*)",
            "/$1",
        ],
    },
)

# Instantiate ApisixRoute component for the learn.mit.edu address
mit_learn_learn_ai_https_apisix_route = OLApisixRoute(
    f"mit-learn-learn-ai-{stack_info.env_suffix}-https-olapisixroute",
    k8s_namespace=learn_ai_namespace,
    k8s_labels=k8s_global_labels,
    route_configs=[
        # Protected route for canvas syllabus agent - requires canvas_token header
        OLApisixRouteConfig(
            route_name="canvas_syllabus_agent",
            priority=20,
            plugins=[
                OLApisixPluginConfig(
                    name="key-auth",
                    config={
                        "header": "canvas_token",
                    },
                ),
            ],
            hosts=[learn_api_domain],
            paths=["/ai/http/canvas_*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # Wildcard route that can use auth but doesn't require it
        OLApisixRouteConfig(
            route_name="passauth",
            priority=2,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin,
                # Use helper from OIDC component instance
                OLApisixPluginConfig(
                    **learn_ai_mit_learn_oidc_resources.get_full_oidc_plugin_config(
                        "pass"
                    )
                ),
            ],
            hosts=[learn_api_domain],
            paths=["/ai/*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # Strip trailing slash from logout redirect
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            plugins=[
                proxy_rewrite_plugin,
                OLApisixPluginConfig(
                    name="redirect",
                    config={
                        "uri": "/logout",  # Redirect within the rewritten path
                    },
                ),
            ],
            hosts=[learn_api_domain],
            paths=["/ai/logout/*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # Routes that require authentication
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin,
                OLApisixPluginConfig(
                    **learn_ai_mit_learn_oidc_resources.get_full_oidc_plugin_config(
                        "auth"
                    )
                ),
            ],
            hosts=[learn_api_domain],
            paths=[
                "/ai/admin/login/*",
                "/ai/http/login/*",
            ],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # WebSocket route for /ai/ws/* paths (using legacy ApisixRoute CRD).
        # Note: This uses ApisixRoute where 'websocket' field IS used to enable
        # WebSocket support. This is different from Gateway API HTTPRoute where
        # websocket support is controlled by Service appProtocol field.
        OLApisixRouteConfig(
            route_name="websocket",
            priority=1,
            websocket=True,  # Required for ApisixRoute to enable WebSocket
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin,
                OLApisixPluginConfig(
                    **learn_ai_mit_learn_oidc_resources.get_full_oidc_plugin_config(
                        "pass"
                    )
                ),
            ],
            hosts=[learn_api_domain],
            paths=[
                "/ai/ws/*",
            ],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_app_k8s, learn_ai_mit_learn_oidc_resources],
    ),
)


# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_tls_v2/
# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
# LEGACY RETIREMENT : goes away
# Won't need this because it will exist from the mit-learn namespace
learn_ai_https_apisix_route = OLApisixRoute(
    f"learn-ai-{stack_info.env_suffix}-https-olapisixroute",
    k8s_namespace=learn_ai_namespace,
    k8s_labels=k8s_global_labels,
    route_configs=[
        # Protected route for canvas syllabus agent - requires canvas_token header
        OLApisixRouteConfig(
            route_name="canvas_syllabus_agent",
            priority=20,
            plugins=[
                OLApisixPluginConfig(
                    name="key-auth",
                    config={
                        "header": "canvas_token",
                    },
                ),
            ],
            hosts=[learn_ai_api_domain],
            paths=["/http/canvas_*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # Wildcard route that can use auth but doesn't require it
        OLApisixRouteConfig(
            route_name="passauth",
            priority=2,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                # Use helper from OIDC component instance
                OLApisixPluginConfig(
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("pass")
                ),
            ],
            hosts=[learn_ai_api_domain],
            paths=["/*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # Strip trailing slash from logout redirect
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            plugins=[
                OLApisixPluginConfig(
                    name="redirect",
                    config={
                        "uri": "/logout",  # Redirect within the rewritten path
                    },
                ),
            ],
            hosts=[learn_ai_api_domain],
            paths=["/logout/*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # Routes that require authentication
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                OLApisixPluginConfig(
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("auth")
                ),
            ],
            hosts=[learn_ai_api_domain],
            paths=[
                "/admin/login/*",
                "/http/login/*",
            ],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # WebSocket route for /ws/* paths (using legacy ApisixRoute CRD).
        # Note: This uses ApisixRoute where 'websocket' field IS used to enable
        # WebSocket support. This is different from Gateway API HTTPRoute where
        # websocket support is controlled by Service appProtocol field.
        OLApisixRouteConfig(
            route_name="websocket",
            priority=1,
            websocket=True,  # Required for ApisixRoute to enable WebSocket
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                OLApisixPluginConfig(
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("pass")
                ),
            ],
            hosts=[learn_ai_api_domain],
            paths=[
                "/ws/*",
            ],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
        ),
        # The sidecar answered this with a 204 (EFF Do Not Track convention
        # for "no policy published"). "passauth" above would otherwise proxy
        # it through to Django, which has no view for it -- kept as a mock so
        # a crawled path doesn't burn a Granian blocking thread on a 404. See
        # docs/plans/remove-nginx-sidecar.md.
        OLApisixRouteConfig(
            route_name="dnt-policy",
            priority=10,
            hosts=[learn_ai_api_domain],
            paths=["/.well-known/dnt-policy.txt"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
            backend_resolve_granularity="service",
            plugins=[
                OLApisixPluginConfig(
                    name="mocking",
                    secretRef=None,
                    config={
                        "response_status": 204,
                        "response_example": "",
                        "content_type": "text/plain",
                        "with_mock_header": False,
                    },
                ),
            ],
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_app_k8s, learn_ai_oidc_resources],
    ),
)

learn_ai_https_cert = OLCertManagerCert(
    f"learn-ai-{stack_info.env_suffix}-https-cert",
    cert_config=OLCertManagerCertConfig(
        application_name="learn-ai",
        k8s_namespace=learn_ai_namespace,
        k8s_labels=k8s_global_labels,
        create_apisixtls_resource=True,
        apisixtls_ingress_class="apache-apisix",
        dest_secret_name="learn-ai-https-cert",  # noqa: S106  # pragma: allowlist secret
        dns_names=[learn_ai_api_domain],
    ),
)
learn_ai_https_apisix_consumer = kubernetes.apiextensions.CustomResource(
    f"learn-ai-{stack_info.env_suffix}-https-apisix-consumer",
    api_version="apisix.apache.org/v2",
    kind="ApisixConsumer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="canvas-agent",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "ingressClassName": "apache-apisix",
        "authParameter": {
            "keyAuth": {
                "value": {
                    "key": Output.secret(
                        read_yaml_secrets(
                            Path(f"vault/secrets.{stack_info.env_suffix}.yaml"),
                        )["learn_ai"]["canvas_syllabus_token"]
                    ),
                },
            },
        },
    },
)

export(
    "learn_ai",
    {
        "rds_host": learn_ai_db.app_db.db_instance.address,
        "redis": redis_cache.address,
        "redis_token": redis_cache.cache_cluster.auth_token,
    },
)
