# ruff: noqa: E501, ERA001
import base64
import json
import mimetypes
import os
import textwrap
from pathlib import Path

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
)
from pulumi_aws import ec2, get_caller_identity, iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_3
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_NGINX_PORT,
    DEFAULT_REDIS_PORT,
    DEFAULT_UWSGI_PORT,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.services import appdb
from ol_infrastructure.components.services.k8s import (
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
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
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

aws_account = get_caller_identity()
stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
monitoring_stack = StackReference("infrastructure.monitoring")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
vector_log_proxy_stack = StackReference(
    f"infrastructure.vector_log_proxy.operations.{stack_info.name}"
)

apps_vpc = network_stack.require_output("applications_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]
learn_ai_environment = f"applications-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": learn_ai_environment},
)
learn_ai_config = Config("learn_ai")
vault_config = Config("vault")

setup_vault_provider(stack_info)
fastly_provider = get_fastly_provider()
github_provider = github.Provider(
    "github_provider",
    owner=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["owner"],
    token=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["token"],
)

k8s_global_labels = {
    "ol.mit.edu/stack": stack_info.full_name,
    "ol.mit.edu/application": "learn-ai",
}
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

learn_ai_app_storage_bucket = s3.BucketV2(
    f"learn-ai-app-storage-bucket-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket_name,
    tags=aws_config.tags,
)

s3.BucketVersioningV2(
    f"learn-ai-app-storage-bucket-versioning-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningV2VersioningConfigurationArgs(
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
vector_log_proxy_fqdn = vector_log_proxy_stack.require_output("vector_log_proxy")[
    "fqdn"
]

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
            name=learn_ai_config.require("frontend_domain"),
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
            content=textwrap.dedent(
                r"""
                {
                # If the request is for the root ("/"), rewrite it to "/frontend/index.html"
                if (req.url == "/" || req.url == "") {
                    set req.url = "/frontend/index.html";
                }

                # If the request does NOT have an extension and is NOT a directory, append ".html"
                if (req.url !~ "\.[a-zA-Z0-9]+$" && req.url !~ "/$") {
                    set req.url = req.url + ".html";
                }

                # Prepend "/frontend" unless it's already prefixed
                if (req.method == "GET" && req.url !~ "^/frontend/") {
                    set req.url = "/frontend" + req.url;
                }
            }
                """
            ),
            type="recv",
        ),
        fastly.ServiceVclSnippetArgs(
            name="Return custom 404 page",
            content=textwrap.dedent(
                r"""
                {
                if ((resp.status == 404 || resp.status == 403) && req.url !~ "^/frontend/404\.html$") {
                    set req.url = "/frontend/404.html";
                    restart;
                }
            }
                """
            ),
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
            url=Output.all(fqdn=vector_log_proxy_fqdn).apply(
                lambda fqdn: "https://{fqdn}".format(**fqdn)
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
redis_cluster_security_group = ec2.SecurityGroup(
    f"learn-ai-redis-cluster-security-group-{stack_info.env_suffix}",
    name_prefix=f"learn-ai-redis-security-group-{stack_info.env_suffix}",
    description="Access control for the learn-ai redis cluster.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[learn_ai_application_security_group.id],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description="Allow application pods to talk to Redis",
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
    engine_version="7.1",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for learn UI tasks and caching.",
    cluster_name=f"learn-ai-redis-{stack_info.env_suffix}",
    subnet_group=apps_vpc["elasticache_subnet"],
    security_groups=[redis_cluster_security_group.id],
    tags=aws_config.tags,
    **defaults(stack_info)["redis"],
)
redis_cache = OLAmazonCache(redis_cache_config)

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
            "REDIS_DOMAIN": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/0",
            "REDIS_SSL_CERT_REQS": "required",
            "CELERY_BROKER_URL": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/0?ssl_cert_reqs=required",
            "CELERY_RESULT_BACKEND": f"rediss://default:{redis_config.require('password')}@{address}:{DEFAULT_REDIS_PORT}/0?ssl_cert_reqs=required",
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
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

# Build a list of not-sensitive env vars for the deployment config
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


application_image_repository_and_tag = f"mitodl/learn-ai-app:{LEARN_AI_DOCKER_TAG}"
image_pull_policy = (
    "Always" if stack_info.env_suffix in ("ci", "qa") else "IfNotPresent"
)
init_containers = [
    # Good Canidates for lib or component functions
    # Run database migrations at startup
    kubernetes.core.v1.ContainerArgs(
        name="migrate",
        image=application_image_repository_and_tag,
        command=["python3", "manage.py", "migrate", "--noinput"],
        image_pull_policy=image_pull_policy,
        env=learn_ai_deployment_env_vars,
        env_from=learn_ai_deployment_envfrom,
    ),
    kubernetes.core.v1.ContainerArgs(
        name="create-cachetable",
        image=application_image_repository_and_tag,
        command=["python3", "manage.py", "createcachetable"],
        image_pull_policy=image_pull_policy,
        env=learn_ai_deployment_env_vars,
        env_from=learn_ai_deployment_envfrom,
    ),
    kubernetes.core.v1.ContainerArgs(
        name="collectstatic",
        image=application_image_repository_and_tag,
        command=["python3", "manage.py", "collectstatic", "--noinput"],
        image_pull_policy=image_pull_policy,
        env=learn_ai_deployment_env_vars,
        env_from=learn_ai_deployment_envfrom,
        volume_mounts=[
            kubernetes.core.v1.VolumeMountArgs(
                name="staticfiles",
                mount_path="/src/staticfiles",
            ),
        ],
    ),
]

# Create a deployment resource to manage the application pods
webapp_labels = k8s_global_labels | {
    "ol.mit.edu/service": "webapp",
    "ol.mit.edu/pod-security-group": "learn-ai-app",
}

learn_ai_webapp_deployment_resource = kubernetes.apps.v1.Deployment(
    f"learn-ai-{stack_info.env_suffix}-webapp-deployment",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="learn-ai-webapp",
        namespace=learn_ai_namespace,
        labels=webapp_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        # TODO @Ardiea: Add horizontal pod autoscaler  # noqa: TD003, FIX002
        replicas=learn_ai_config.get_int("webapp_replica_count") or 2,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=webapp_labels,
        ),
        # Limits the chances of simulatious pod restarts -> db migrations (hopefully)
        strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
            type="RollingUpdate",
            rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                max_surge=0,
                max_unavailable=1,
            ),
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=webapp_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                volumes=[
                    kubernetes.core.v1.VolumeArgs(
                        name="staticfiles",
                        empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
                    ),
                    kubernetes.core.v1.VolumeArgs(
                        name="nginx-config",
                        config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                            name=learn_ai_nginx_configmap.metadata.name,
                            items=[
                                kubernetes.core.v1.KeyToPathArgs(
                                    key="web.conf",
                                    path="web.conf",
                                ),
                            ],
                        ),
                    ),
                ],
                service_account_name=learn_ai_service_account_name,
                init_containers=init_containers,
                dns_policy="ClusterFirst",
                containers=[
                    # nginx container infront of uwsgi
                    kubernetes.core.v1.ContainerArgs(
                        name="nginx",
                        image="nginx:1.9.5",
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=DEFAULT_NGINX_PORT,
                            )
                        ],
                        image_pull_policy=image_pull_policy,
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "50m", "memory": "64Mi"},
                            limits={"cpu": "100m", "memory": "128Mi"},
                        ),
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="staticfiles",
                                mount_path="/src/staticfiles",
                            ),
                            kubernetes.core.v1.VolumeMountArgs(
                                name="nginx-config",
                                mount_path="/etc/nginx/conf.d/web.conf",
                                sub_path="web.conf",
                                read_only=True,
                            ),
                        ],
                    ),
                    # Actual application run with uwsgi
                    kubernetes.core.v1.ContainerArgs(
                        name="learn-ai-app",
                        image=application_image_repository_and_tag,
                        command=[
                            "uvicorn",
                            "main.asgi:application",
                            "--reload",
                            "--host",
                            "0.0.0.0",  # noqa: S104
                            "--port",
                            f"{DEFAULT_UWSGI_PORT}",
                        ],
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=DEFAULT_UWSGI_PORT
                            )
                        ],
                        image_pull_policy=image_pull_policy,
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "250m", "memory": "1000Mi"},
                            limits={"cpu": "500m", "memory": "1600Mi"},
                        ),
                        env=learn_ai_deployment_env_vars,
                        env_from=learn_ai_deployment_envfrom,
                        volume_mounts=[
                            kubernetes.core.v1.VolumeMountArgs(
                                name="staticfiles",
                                mount_path="/src/staticfiles",
                            ),
                        ],
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_db, db_creds_secret, redis_creds],
    ),
)

celery_labels = k8s_global_labels | {
    "ol.mit.edu/service": "celery",
    "ol.mit.edu/pod-security-group": "learn-ai-app",
}
learn_ai_celery_deployment_resource = kubernetes.apps.v1.Deployment(
    f"learn-ai-{stack_info.env_suffix}-celery-deployment",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="learn-ai-celery",
        namespace=learn_ai_namespace,
        labels=celery_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=learn_ai_config.get_int("celery_replica_count") or 2,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=celery_labels,
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=celery_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name=learn_ai_service_account_name,
                dns_policy="ClusterFirst",
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="celery-worker",
                        image=application_image_repository_and_tag,
                        command=[
                            "celery",
                            "-A",
                            "main.celery:app",
                            "worker",
                            "-E",
                            "-Q",
                            "default,edx_content",
                            "-B",
                            "-l",
                            "INFO",
                        ],
                        env=learn_ai_deployment_env_vars,
                        env_from=learn_ai_deployment_envfrom,
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "250m", "memory": "1000Mi"},
                            limits={"cpu": "500m", "memory": "1600Mi"},
                        ),
                    )
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_db, db_creds_secret, redis_creds],
    ),
)

# A kubernetes service resource to act as load balancer for the app instances
learn_ai_service_name = "learn-ai-webapp"
learn_ai_service_port_name = "http"
learn_ai_service = kubernetes.core.v1.Service(
    f"learn-ai-{stack_info.env_suffix}-service",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=learn_ai_service_name,
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        selector=webapp_labels,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name=learn_ai_service_port_name,
                port=DEFAULT_NGINX_PORT,
                target_port=DEFAULT_NGINX_PORT,
                protocol="TCP",
            ),
        ],
        type="ClusterIP",
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

# Good canidate for a component resource
learn_ai_pod_security_group_policy = (
    kubernetes.apiextensions.CustomResource(
        f"learn-ai-{stack_info.env_suffix}-application-pod-security-group-policy",
        api_version="vpcresources.k8s.aws/v1beta1",
        kind="SecurityGroupPolicy",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="learn-ai-app",
            namespace=learn_ai_namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "podSelector": {
                "matchLabels": {"ol.mit.edu/pod-security-group": "learn-ai-app"},
            },
            "securityGroups": {
                "groupIds": [
                    learn_ai_application_security_group.id,
                ],
            },
        },
    ),
)

# Create the apisix custom resources since it doesn't support gateway-api yet

# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_plugin_config/
# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_pluginconfig_v2/

# LEGACY RETIREMENT : goes away
# Load open-id-connect secrets into a k8s secret via VSO
oidc_secret_name = "oidc-secrets"  # pragma: allowlist secret  # noqa: S105
oidc_secret = OLVaultK8SSecret(
    name=f"learn-ai-{stack_info.env_suffix}-oidc-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="oidc-static-secrets",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
        dest_secret_name=oidc_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/learn-ai",
        excludes=[".*"],
        exclude_raw=True,
        # Refresh frequently because substructure keycloak stack could change some of these
        refresh_after="1m",
        templates={
            "client_id": '{{ get .Secrets "client_id" }}',
            "client_secret": '{{ get .Secrets "client_secret" }}',
            "realm": '{{ get .Secrets "realm_name" }}',
            "discovery": '{{ get .Secrets "url" }}/.well-known/openid-configuration',
            "session.secret": '{{ get .Secrets "secret" }}',
        },
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

mit_learn_oidc_secret_name = (
    "mit-learn-oidc-secrets"  # pragma: allowlist secret # noqa: S105
)
mit_learn_oidc_secret = OLVaultK8SSecret(
    f"ol-mitlearn-oidc-secrets-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="mit-learn-oidc-static-secrets",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
        dest_secret_name=mit_learn_oidc_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/mitlearn",
        excludes=[".*"],
        exclude_raw=True,
        # Refresh frequently because substructure keycloak stack could change some of these
        refresh_after="1m",
        templates={
            "client_id": '{{ get .Secrets "client_id" }}',
            "client_secret": '{{ get .Secrets "client_secret" }}',
            "realm": '{{ get .Secrets "realm_name" }}',
            "discovery": '{{ get .Secrets "url" }}/.well-known/openid-configuration',
            "session.secret": '{{ get .Secrets "secret" }}',
        },
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

# Good canidates for a component resource
learn_ai_shared_plugins = OLApisixSharedPlugins(
    f"learn-ai-{stack_info.env_suffix}-shared-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="learn-ai",
        resource_suffix="ol-shared-plugins",
        k8s_namespace=learn_ai_namespace,
        k8s_labels=k8s_global_labels,
        enable_defaults=True,
    ),
)

# Ref: https://apisix.apache.org/docs/apisix/plugins/openid-connect/
base_oidc_plugin_config = {
    "scope": "openid profile ol-profile",
    "bearer_only": False,
    "introspection_endpoint_auth_method": "client_secret_post",
    "ssl_verify": False,
    "renew_access_token_on_expiry": True,
    "session": {"cookie": {"lifetime": 60 * 20160}},
    "session_contents": {
        "access_token": True,
        "enc_id_token": True,
        "id_token": True,
        "user": True,
    },
    "logout_path": "/logout",
    "post_logout_redirect_uri": "/",
}

learn_ai_api_domain = learn_ai_config.require("backend_domain")
learn_api_domain = learn_ai_config.require("learn_backend_domain")

# ApisixUpstream resources don't seem to work but we don't really need them?
# Ref: https://github.com/apache/apisix-ingress-controller/issues/1655
# Ref: https://github.com/apache/apisix-ingress-controller/issues/1855

# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_route_v2/
# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/
# LEGACY RETIREMENT : goes away
learn_ai_https_apisix_route = kubernetes.apiextensions.CustomResource(
    f"learn-ai-{stack_info.env_suffix}-https-apisix-route",
    api_version="apisix.apache.org/v2",
    kind="ApisixRoute",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="learn-ai-https",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "http": [
            {
                # Wildcard route that can use auth but doesn't require it
                "name": "passauth",
                "priority": 2,
                "plugin_config_name": learn_ai_shared_plugins.resource_name,
                "plugins": [
                    {
                        "name": "openid-connect",
                        "enable": True,
                        "secretRef": oidc_secret_name,
                        "config": base_oidc_plugin_config | {"unauth_action": "pass"},
                    }
                ],
                "match": {
                    "hosts": [learn_ai_api_domain],
                    "paths": [
                        "/*",
                    ],
                },
                "backends": [
                    {
                        "serviceName": learn_ai_service_name,
                        "servicePort": learn_ai_service_port_name,
                        "resolveGranularity": "service",
                    },
                ],
            },
            {
                # Strip trailing slash from logout redirect
                "name": "logout-redirect",
                "priority": 10,
                "plugins": [
                    {
                        "name": "redirect",
                        "enable": True,
                        "config": {
                            "uri": "/logout",
                        },
                    },
                ],
                "match": {
                    "hosts": [learn_ai_api_domain],
                    "paths": [
                        "/logout/*",
                    ],
                },
                "backends": [
                    {
                        "serviceName": learn_ai_service_name,
                        "servicePort": learn_ai_service_port_name,
                        "resolveGranularity": "service",
                    },
                ],
            },
            {
                # Routes that require authentication
                "name": "reqauth",
                "priority": 10,
                "plugin_config_name": learn_ai_shared_plugins.resource_name,
                "plugins": [
                    {
                        "name": "openid-connect",
                        "enable": True,
                        "secretRef": oidc_secret_name,
                        "config": base_oidc_plugin_config | {"unauth_action": "auth"},
                    }
                ],
                "match": {
                    "hosts": [
                        learn_ai_api_domain,
                    ],
                    "paths": [
                        "/admin/login/*",
                        "/http/login/*",
                    ],
                },
                "backends": [
                    {
                        "serviceName": learn_ai_service_name,
                        "servicePort": learn_ai_service_port_name,
                        "resolveGranularity": "service",
                    },
                ],
            },
            {
                # Sepcial handling for websocket URLS.
                "name": "websocket",
                "priority": 1,
                "websocket": True,
                "plugin_config_name": learn_ai_shared_plugins.resource_name,
                "plugins": [
                    {
                        "name": "openid-connect",
                        "enable": True,
                        "secretRef": oidc_secret_name,
                        "config": base_oidc_plugin_config | {"unauth_action": "pass"},
                    }
                ],
                "match": {
                    "hosts": [
                        learn_ai_api_domain,
                    ],
                    "paths": [
                        "/ws/*",
                    ],
                },
                "backends": [
                    {
                        "serviceName": learn_ai_service_name,
                        "servicePort": learn_ai_service_port_name,
                        "resolveGranularity": "service",
                    },
                ],
            },
        ],
    },
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_service, oidc_secret],
    ),
)

learn_ai_oidc_resources = OLApisixOIDCResources(
    f"learn-ai-{stack_info.env_suffix}-oidc-resources",
    oidc_config=OLApisixOIDCConfig(
        application_name="learn-ai",
        k8s_labels=k8s_global_labels,
        k8s_namespace=learn_ai_namespace,
        oidc_logout_path="/logout",  # maybe `/ai/logout` ????
        oidc_post_logout_redirect_uri="/",
        oidc_use_session_secret=True,
        vault_mount="secret-operations",
        vault_mount_type="kv-v1",
        vault_path="sso/mitlearn",
        vaultauth=vault_k8s_resources.auth_name,
    ),
)

proxy_rewrite_plugin_config = {
    "name": "proxy-rewrite",
    "enable": True,
    "config": {
        "regex_uri": [
            "/ai/(.*)",
            "/$1",
        ],
    },
}

# New ApisixRoute object for the learn.mit.edu address
# All paths prefixed with /ai
# Host match is only the mit-learn domain
mit_learn_learn_ai_https_apisix_route = OLApisixRoute(
    name=f"mit-learn-learn-ai-{stack_info.env_suffix}-https-olapisixroute",
    k8s_namespace=learn_ai_namespace,
    k8s_labels=k8s_global_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="passauth",
            priority=2,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                learn_ai_oidc_resources.get_full_oidc_plugin_config("pass"),
            ],
            hosts=[learn_api_domain],
            paths=["/ai/*"],
            backend_service_name=learn_ai_service_name,
            backend_service_port=learn_ai_service_port_name,
            backend_resolve_granularity="service",
        ),
        OLApisixRouteConfig(
            route_name="logout-redirect",
            priority=10,
            # shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                {
                    "name": "redirect",
                    "enable": True,
                    "config": {
                        "uri": "/logout",
                    },
                },
            ],
            hosts=[learn_api_domain],
            paths=["/ai/logout/*"],
            backend_service_name=learn_ai_service_name,
            backend_service_port=learn_ai_service_port_name,
            backend_resolve_granularity="service",
        ),
        OLApisixRouteConfig(
            route_name="reqauth",
            priority=10,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                learn_ai_oidc_resources.get_full_oidc_plugin_config("auth"),
            ],
            hosts=[learn_api_domain],
            paths=[
                "/ai/admin/login/*",
                "/ai/http/login/*",
            ],
            backend_service_name=learn_ai_service_name,
            backend_service_port=learn_ai_service_port_name,
            backend_resolve_granularity="service",
        ),
        OLApisixRouteConfig(
            route_name="websocket",
            priority=1,
            websocket=True,
            shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
            plugins=[
                proxy_rewrite_plugin_config,
                learn_ai_oidc_resources.get_full_oidc_plugin_config("pass"),
            ],
            hosts=[learn_api_domain],
            paths=[
                "/ai/ws/*",
            ],
            backend_service_name=learn_ai_service_name,
            backend_service_port=learn_ai_service_port_name,
            backend_resolve_granularity="service",
        ),
    ],
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[learn_ai_service, mit_learn_oidc_secret],
    ),
)

# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_tls_v2/
# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
# LEGACY RETIREMENT : goes away
# Won't need this because it will exist from the mit-learn namespace
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
        # Use the shared ol-wildcard cert loaded into every cluster
        "secret": {
            "name": "ol-wildcard-cert",
            "namespace": "operations",
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

gh_workflow_api_base_env_var = github.ActionsVariable(
    f"learn-ai-gh-workflow-api-base-env-variablet-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"API_BASE_{env_var_suffix}",  # pragma: allowlist secret
    value=f"https://{learn_api_domain}/ai",
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
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
