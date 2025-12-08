# ruff: noqa: E501
"""Learn AI application infrastructure deployment (Pulumi)."""

import base64
import json
import mimetypes
import os
import textwrap
from pathlib import Path

import pulumi_consul as consul
import pulumi_fastly as fastly
import pulumi_github as github
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    InvokeOptions,
    Output,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import ec2, get_caller_identity, iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_3
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_REDIS_PORT,
    DEFAULT_WSGI_PORT,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.services import appdb
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
    OLApplicationK8s,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    KubernetesServiceAppProtocol,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

aws_account = get_caller_identity()
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
cluster_substructure_stack = StackReference(
    f"substructure.aws.eks.applications.{stack_info.name}"
)
dns_stack = StackReference("infrastructure.aws.dns")
monitoring_stack = StackReference("infrastructure.monitoring")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
vector_log_proxy_stack = StackReference(
    f"infrastructure.vector_log_proxy.operations.{stack_info.name}"
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
github_provider = github.Provider(
    "github_provider",
    owner=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["owner"],
    token=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["token"],
)

k8s_global_labels = K8sGlobalLabels(
    ou=BusinessUnit.mit_learn, service=Services.mit_learn, stack=stack_info
).model_dump()
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Fail hard if LEARN_AI_DOCKER_TAG is not set
if "LEARN_AI_DOCKER_TAG" not in os.environ:
    msg = "LEARN_AI_DOCKER_TAG must be set"
    raise OSError(msg)
LEARN_AI_DOCKER_TAG = os.getenv("LEARN_AI_DOCKER_TAG")

match stack_info.env_suffix:
    case "production":
        env_var_suffix = "PROD"
    case "qa":
        env_var_suffix = "RC"
    case "ci":
        env_var_suffix = "CI"
    case _:
        env_var_suffix = "INVALID"

learn_ai_namespace = "learn-ai"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(learn_ai_namespace, ns)
)

ol_zone_id = dns_stack.require_output("ol")["id"]

################################################
# Frontend storage bucket
learn_ai_app_storage_bucket_name = f"ol-mit-learn-ai-{stack_info.env_suffix}"

learn_ai_app_storage_bucket = s3.Bucket(
    f"learn-ai-app-storage-bucket-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket_name,
    tags=aws_config.tags,
)

s3.BucketVersioning(
    f"learn-ai-app-storage-bucket-versioning-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled",
    ),
)

learn_ai_app_storage_bucket_ownership_controls = s3.BucketOwnershipControls(
    f"learn-ai-app-storage-bucket-ownership-controls-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)

learn_ai_app_storage_bucket_public_access = s3.BucketPublicAccessBlock(
    f"learn-ai-app-storage-bucket-public-access-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)

learn_ai_app_storage_bucket_policy = s3.BucketPolicy(
    f"learn-ai-app-storage-bucket-policy-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket.id,
    policy=learn_ai_app_storage_bucket.arn.apply(
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
    opts=ResourceOptions(
        depends_on=[
            learn_ai_app_storage_bucket_public_access,
            learn_ai_app_storage_bucket_ownership_controls,
        ]
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


gh_workflow_s3_bucket_permissions_doc = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Action": [
                "s3:ListBucket*",
            ],
            "Effect": "Allow",
            "Resource": [f"arn:aws:s3:::{learn_ai_app_storage_bucket_name}"],
        },
        {
            "Action": [
                "s3:GetObject*",
                "s3:PutObject",
                "s3:PutObjectAcl",
                "s3:DeleteObject",
            ],
            "Effect": "Allow",
            "Resource": [f"arn:aws:s3:::{learn_ai_app_storage_bucket_name}/frontend/*"],
        },
    ],
}

gh_workflow_iam_policy = iam.Policy(
    f"learn-ai-gh-workflow-iam-policy-{stack_info.env_suffix}",
    name=f"learn-ai-gh-workflow-iam-policy-{stack_info.env_suffix}",
    policy=lint_iam_policy(
        gh_workflow_s3_bucket_permissions_doc,
        stringify=True,
        parliament_config=parliament_config,
    ),
)

################################################
# Github frontend workflow IAM configuration
# Just create a static user for now. Some day refactor to use
# https://github.com/hashicorp/vault-action
gh_workflow_user = iam.User(
    f"learn-ai-gh-workflow-user-{stack_info.env_suffix}",
    name=f"learn-ai-gh-workflow-{stack_info.env_suffix}",
    tags=aws_config.tags,
)
iam.PolicyAttachment(
    f"learn-ai-gh-workflow-iam-policy-attachment-{stack_info.env_suffix}",
    policy_arn=gh_workflow_iam_policy.arn,
    users=[gh_workflow_user.name],
)
gh_workflow_accesskey = iam.AccessKey(
    f"learn-ai-gh-workflow-access-key-{stack_info.env_suffix}",
    user=gh_workflow_user.name,
    status="Active",
)
gh_repo = github.get_repository(
    full_name="mitodl/learn-ai",
    opts=InvokeOptions(provider=github_provider),
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
            address=learn_ai_app_storage_bucket.bucket_domain_name,
            name="learn-ai",
            override_host=learn_ai_app_storage_bucket.bucket_domain_name,
            port=DEFAULT_HTTPS_PORT,
            ssl_cert_hostname=learn_ai_app_storage_bucket.bucket_domain_name,
            ssl_sni_hostname=learn_ai_app_storage_bucket.bucket_domain_name,
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
            comment=f"{stack_info.env_prefix} {stack_info.env_suffix} Application",
            name=learn_ai_frontend_domain,
        ),
    ],
    request_settings=[
        fastly.ServiceVclRequestSettingArgs(
            force_ssl=True,
            name="Generated by force TLS and enable HSTS",
            xff="",
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
            name=f"fastly-{stack_info.env_prefix}-{stack_info.env_suffix}-https-logging-args",
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
    data_json=json.dumps(learn_ai_vault_secrets),
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

# Load the nginx configuration into a configmap
learn_ai_nginx_configmap = kubernetes.core.v1.ConfigMap(
    f"learn-ai-{stack_info.env_suffix}-nginx-configmap",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="nginx-config",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
    ),
    data={
        "web.conf": Path(__file__).parent.joinpath("files/web.conf").read_text(),
    },
)

if learn_ai_config.get_bool("use_granian"):
    cmd_array = ["granian"]
    arg_array = [
        "--interface",
        "asgi",
        "--host",
        "0.0.0.0",  # noqa: S104
        "--port",
        f"{DEFAULT_WSGI_PORT}",
        "--workers",
        "1",
        "--runtime-threads",
        "2",
        "--log-level",
        "debug",
        "main.asgi:application",
    ]
else:
    cmd_array = ["uvicorn"]
    arg_array = [
        "main.asgi:application",
        "--reload",
        "--host",
        "0.0.0.0",  # noqa: S104
        "--port",
        f"{DEFAULT_WSGI_PORT}",
    ]

# Instantiate the OLApplicationK8s component
learn_ai_app_k8s = OLApplicationK8s(
    ol_app_k8s_config=OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=learn_ai_config.require_object("env_vars") or {},
        application_name="learn-ai",
        application_namespace=learn_ai_namespace,
        application_lb_service_name="learn-ai-webapp",
        application_lb_service_port_name="http",
        application_lb_service_app_protocol=KubernetesServiceAppProtocol.WSS,
        k8s_global_labels=k8s_global_labels,
        env_from_secret_names=[
            db_creds_secret_name,
            redis_creds_secret_name,
            static_secrets_name,
        ],
        application_security_group_id=learn_ai_application_security_group.id,
        # Use the fixed name used in the SecurityGroupPolicy spec
        application_security_group_name=Output.from_input("learn-ai-app"),
        application_service_account_name=learn_ai_service_account.metadata.name,
        application_image_repository="mitodl/learn-ai-app",
        application_docker_tag=LEARN_AI_DOCKER_TAG,
        application_min_replicas=learn_ai_config.get("min_replicas") or 2,
        application_cmd_array=cmd_array,
        application_arg_array=arg_array,
        slack_channel=slack_channel,
        vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
        import_nginx_config=True,
        # Nginx resources (defaults from component are fine)
        # App container resources
        resource_requests={"cpu": "250m", "memory": "1600Mi"},
        resource_limits={"memory": "1600Mi"},
        init_migrations=True,
        init_collectstatic=True,  # Assuming createcachetable is not needed or handled elsewhere
        celery_worker_configs=[
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="default",
                redis_host=redis_cache.address,
                redis_database_index="1",
                redis_password=redis_config.require("password"),
                resource_requests={"cpu": "100m", "memory": "2500Mi"},
                resource_limits={"memory": "2500Mi"},
            ),
            OLApplicationK8sCeleryWorkerConfig(
                queue_name="edx_content",
                redis_host=redis_cache.address,
                redis_database_index="1",
                redis_password=redis_config.require("password"),
                resource_requests={"cpu": "100m", "memory": "2500Mi"},
                resource_limits={"memory": "2500Mi"},
            ),
        ],
        hpa_scaling_metrics=[
            kubernetes.autoscaling.v2.MetricSpecArgs(
                type="Resource",
                resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                    name="cpu",
                    target=kubernetes.autoscaling.v2.MetricTargetArgs(
                        type="Utilization",
                        average_utilization=60,  # Target CPU utilization (60%)
                    ),
                ),
            ),
            # Scale up when avg usage exceeds: 1800 * 0.8 = 1440 Mi
            kubernetes.autoscaling.v2.MetricSpecArgs(
                type="Resource",
                resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                    name="memory",
                    target=kubernetes.autoscaling.v2.MetricTargetArgs(
                        type="Utilization",
                        average_utilization=80,  # Target memory utilization (80%)
                    ),
                ),
            ),
        ],
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[
            learn_ai_db,
            db_creds_secret,
            redis_creds,
            static_secrets,
            vault_k8s_resources,
            learn_ai_application_security_group,
        ],
    ),
)

# Reconstruct variables needed for Celery deployment
application_image_repository_and_tag = f"mitodl/learn-ai-app:{LEARN_AI_DOCKER_TAG}"

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
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

# Instantiate OIDC resources component for mit-learn domain
learn_ai_oidc_resources = OLApisixOIDCResources(
    f"learn-ai-{stack_info.env_suffix}-oidc-resources",
    oidc_config=OLApisixOIDCConfig(
        application_name="learn-ai",
        k8s_labels=k8s_global_labels,
        k8s_namespace=learn_ai_namespace,
        oidc_scope="openid profile email",  # Default scope from component
        oidc_introspection_endpoint_auth_method="client_secret_basic",  # Default
        oidc_logout_path="/logout",
        oidc_post_logout_redirect_uri="/",
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
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("pass")
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
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("auth")
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
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("pass")
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
        depends_on=[learn_ai_app_k8s, learn_ai_oidc_resources],
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
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_app_k8s, learn_ai_oidc_resources],
    ),
)
# Gateway API HTTPRoute for learn.mit.edu domain (Phase 3 Migration)
# This runs in parallel with ApisixRoute during migration
mit_learn_learn_ai_https_http_route = OLApisixHTTPRoute(
    f"mit-learn-learn-ai-{stack_info.env_suffix}-https-httproute",
    route_configs=[
        # Protected route for canvas syllabus agent - requires canvas_token header
        OLApisixHTTPRouteConfig(
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
        ),
        # Wildcard route that can use auth but doesn't require it
        OLApisixHTTPRouteConfig(
            route_name="passauth",
            priority=2,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin,
                # Use helper from OIDC component instance
                OLApisixPluginConfig(
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("pass")
                ),
            ],
            hosts=[learn_api_domain],
            paths=["/ai/*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
        ),
        # Strip trailing slash from logout redirect
        OLApisixHTTPRouteConfig(
            route_name="logout-redirect",
            priority=10,
            plugins=[
                proxy_rewrite_plugin,
                OLApisixPluginConfig(
                    name="redirect",
                    config={
                        "uri": "/logout",
                    },
                ),
            ],
            hosts=[learn_api_domain],
            paths=["/ai/logout/*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
        ),
        # Routes that require authentication
        OLApisixHTTPRouteConfig(
            route_name="reqauth",
            priority=10,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin,
                OLApisixPluginConfig(
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("auth")
                ),
            ],
            hosts=[learn_api_domain],
            paths=[
                "/ai/admin/login/*",
                "/ai/http/login/*",
            ],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
        ),
        # WebSocket route for /ai/ws/* paths.
        # Note: Gateway API HTTPRoute handles WebSocket via HTTP Upgrade protocol.
        # Service appProtocol (kubernetes.io/ws or wss) is OPTIONAL - it's a hint
        # to the gateway that may optimize behavior (timeouts, health checks) but
        # does NOT restrict traffic. Regular HTTP requests work normally even with
        # appProtocol set. Most gateways auto-detect WebSocket upgrades without it.
        # The 'websocket' and 'priority' fields below are for ApisixRoute compatibility
        # only and have NO effect on HTTPRoute. Route precedence is by path specificity.
        # See: https://gateway-api.sigs.k8s.io/geps/gep-1911/
        OLApisixHTTPRouteConfig(
            route_name="websocket",
            priority=1,  # NOT used in HTTPRoute - precedence by path specificity
            websocket=True,  # NOT used in HTTPRoute - set Service appProtocol instead
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin,
                OLApisixPluginConfig(
                    **learn_ai_oidc_resources.get_full_oidc_plugin_config("pass")
                ),
            ],
            hosts=[learn_api_domain],
            paths=[
                "/ai/ws/*",
            ],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
        ),
    ],
    k8s_namespace=learn_ai_namespace,
    k8s_labels=k8s_global_labels,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_app_k8s, learn_ai_oidc_resources],
    ),
)

# Gateway API HTTPRoute for legacy backend_domain (Phase 3 Migration)
# This runs in parallel with ApisixRoute during migration
learn_ai_https_http_route = OLApisixHTTPRoute(
    f"learn-ai-{stack_info.env_suffix}-https-httproute",
    route_configs=[
        # Protected route for canvas syllabus agent - requires canvas_token header
        OLApisixHTTPRouteConfig(
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
        ),
        # Wildcard route that can use auth but doesn't require it
        OLApisixHTTPRouteConfig(
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
        ),
        # Strip trailing slash from logout redirect
        OLApisixHTTPRouteConfig(
            route_name="logout-redirect",
            priority=10,
            plugins=[
                OLApisixPluginConfig(
                    name="redirect",
                    config={
                        "uri": "/logout",
                    },
                ),
            ],
            hosts=[learn_ai_api_domain],
            paths=["/logout/*"],
            backend_service_name=learn_ai_app_k8s.application_lb_service_name,
            backend_service_port=learn_ai_app_k8s.application_lb_service_port_name,
        ),
        # Routes that require authentication
        OLApisixHTTPRouteConfig(
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
        ),
        # WebSocket route for /ws/* paths.
        # Note: Gateway API HTTPRoute handles WebSocket via HTTP Upgrade protocol.
        # Service appProtocol (kubernetes.io/ws or wss) is OPTIONAL - it's a hint
        # to the gateway that may optimize behavior but does NOT restrict traffic.
        # Regular HTTP requests work normally even with appProtocol set. The
        # 'websocket' and 'priority' fields are for ApisixRoute compatibility only.
        # See: https://gateway-api.sigs.k8s.io/geps/gep-1911/
        OLApisixHTTPRouteConfig(
            route_name="websocket",
            priority=1,  # NOT used in HTTPRoute - precedence by path specificity
            websocket=True,  # NOT used in HTTPRoute - set Service appProtocol instead
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
        ),
    ],
    k8s_namespace=learn_ai_namespace,
    k8s_labels=k8s_global_labels,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_app_k8s, learn_ai_oidc_resources],
    ),
)

learn_ai_https_apisix_tls = kubernetes.apiextensions.CustomResource(
    f"learn-ai-{stack_info.env_suffix}-https-apisix-tls",
    api_version="apisix.apache.org/v2",
    kind="ApisixTls",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="learn-ai-https",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "hosts": [learn_ai_api_domain],
        "ingressClassName": "apache-apisix",
        # Use the shared ol-wildcard cert loaded into every cluster
        "secret": {
            "name": "ol-wildcard-cert",
            "namespace": "operations",
        },
    },
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

# Finally, put the aws access key into the github actions configuration

gh_workflow_access_key_id_env_secret = github.ActionsSecret(
    f"learn-ai-gh-workflow-access-key-id-env-secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"AWS_ACCESS_KEY_ID_{env_var_suffix}",  # pragma: allowlist secret
    plaintext_value=gh_workflow_accesskey.id,
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_secretaccesskey_env_secret = github.ActionsSecret(
    f"learn-ai-gh-workflow-secretaccesskey-env-secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"AWS_SECRET_ACCESS_KEY_{env_var_suffix}",  # pragma: allowlist secret
    plaintext_value=gh_workflow_accesskey.secret,
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)

gh_workflow_fastly_api_key_env_secret = github.ActionsSecret(
    f"learn-ai-gh-workflow-fastly-api-key-env-secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"FASTLY_API_KEY_{env_var_suffix}",  # pragma: allowlist secret
    plaintext_value=read_yaml_secrets(Path("fastly.yaml"))["admin_api_key"],
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_fastly_service_id_env_secret = github.ActionsSecret(
    f"learn-ai-gh-workflow-fastly-service-id-env-secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"FASTLY_SERVICE_ID_{env_var_suffix}",  # pragma: allowlist secret
    plaintext_value=learn_ai_fastly_service.id,
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)

gh_workflow_s3_bucket_name_env_secret = github.ActionsVariable(
    f"learn-ai-gh-workflow-s3-bucket-name-env-variable-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"AWS_S3_BUCKET_NAME_{env_var_suffix}",  # pragma: allowlist secret
    value=learn_ai_app_storage_bucket_name,
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)

frontend_vars = learn_ai_config.require_object("frontend_vars")
# Variables for frontend app build
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-api-base-env-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"API_BASE_{env_var_suffix}",  # pragma: allowlist secret
    value=f"https://{learn_api_domain}/ai",
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-ai-csrf-cookie-name-env-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"AI_CSRF_COOKIE_NAME_{env_var_suffix}",  # pragma: allowlist secret
    value="csrftoken",
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-learn-mit-ai-login-url-env-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"MIT_LEARN_AI_LOGIN_URL_{env_var_suffix}",  # pragma: allowlist secret
    value=f"https://{learn_api_domain}/ai/http/login/",
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-mit-learn-api-base-url-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"MIT_LEARN_API_BASE_URL_{env_var_suffix}",  # pragma: allowlist secret
    value=f"https://{learn_api_domain}/learn/",
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-mit-learn-app-base-url-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"MIT_LEARN_APP_BASE_URL_{env_var_suffix}",  # pragma: allowlist secret
    value=frontend_vars["MIT_LEARN_APP_BASE_URL"],
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-openedx-api-base-url-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"OPENEDX_API_BASE_URL_{env_var_suffix}",  # pragma: allowlist secret
    value=frontend_vars["OPENEDX_API_BASE_URL"],
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-openedx-login-url-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"OPENEDX_LOGIN_URL_{env_var_suffix}",  # pragma: allowlist secret
    value=frontend_vars["OPENEDX_LOGIN_URL"],
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-ai-elasticsearch-url-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"MIT_SEARCH_ELASTIC_URL_{env_var_suffix}",  # pragma: allowlist secret
    value=frontend_vars["MIT_SEARCH_ELASTIC_URL"],
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-ai-vectorsearch-url-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"MIT_SEARCH_VECTOR_URL_{env_var_suffix}",  # pragma: allowlist secret
    value=frontend_vars["MIT_SEARCH_VECTOR_URL"],
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)

xpro_consul_opts = get_consul_provider(
    stack_info=stack_info,
    consul_address=f"https://consul-xpro-{stack_info.env_suffix}.odl.mit.edu",
    provider_name=f"consul-provider-xpro-{stack_info.env_suffix}",
)
consul.Keys(
    f"learn-api-domain-consul-key-for-xpro-openedx-{stack_info.env_suffix}",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/learn-ai-frontend-domain",
            delete=False,
            value=learn_ai_frontend_domain,
        )
    ],
    opts=xpro_consul_opts,
)

mitxonline_consul_opts = get_consul_provider(
    stack_info,
    consul_address=f"https://consul-mitxonline-{stack_info.env_suffix}.odl.mit.edu",
    provider_name=f"consul-provider-mitxonline-{stack_info.env_suffix}",
)
consul.Keys(
    "learn-api-domain-consul-key-for-mitxonline-openedx",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/learn-ai-frontend-domain",
            delete=False,
            value=learn_ai_frontend_domain,
        )
    ],
    opts=mitxonline_consul_opts,
)

mitx_consul_opts = get_consul_provider(
    stack_info,
    consul_address=f"https://consul-mitx-{stack_info.env_suffix}.odl.mit.edu",
    provider_name=f"consul-provider-mitx-{stack_info.env_suffix}",
)
consul.Keys(
    "learn-api-domain-consul-key-for-mitx-openedx",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/learn-ai-frontend-domain",
            delete=False,
            value=learn_ai_frontend_domain,
        )
    ],
    opts=mitx_consul_opts,
)

mitx_staging_consul_opts = get_consul_provider(
    stack_info,
    consul_address=f"https://consul-mitx-staging-{stack_info.env_suffix}.odl.mit.edu",
    provider_name=f"consul-provider-mitx-staging-{stack_info.env_suffix}",
)
consul.Keys(
    "learn-api-domain-consul-key-for-mitx-staging-openedx",
    keys=[
        consul.KeysKeyArgs(
            path="edxapp/learn-ai-frontend-domain",
            delete=False,
            value=learn_ai_frontend_domain,
        )
    ],
    opts=mitx_staging_consul_opts,
)

if stack_info.env_suffix != "ci":
    gh_workflow_posthog_project_api_key_env_secret = github.ActionsSecret(
        f"learn-ai-gh-workflow-posthog-project-api_key-{stack_info.env_suffix}",
        repository=gh_repo.name,
        secret_name=f"POSTHOG_PROJECT_API_KEY_{env_var_suffix}",
        plaintext_value=mitlearn_posthog_secrets["project_api_key"],
        opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
    )
    gh_workflow_posthog_personal_api_key_env_secret = github.ActionsSecret(
        f"learn-ai-gh-workflow-posthog-personal-api-key-{stack_info.env_suffix}",
        repository=gh_repo.name,
        secret_name=f"POSTHOG_PERSONAL_API_KEY_{env_var_suffix}",
        plaintext_value=mitlearn_posthog_secrets["personal_api_key"],
        opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
    )

export(
    "learn_ai",
    {
        "rds_host": learn_ai_db.app_db.db_instance.address,
        "redis": redis_cache.address,
        "redis_token": redis_cache.cache_cluster.auth_token,
    },
)
