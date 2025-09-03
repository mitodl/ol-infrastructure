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
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.constants import FASTLY_A_TLS_1_3
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_REDIS_PORT,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.services import appdb
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.cache_helper import CacheInstanceTypes
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
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

fastly_provider = get_fastly_provider()
github_provider = github.Provider(
    "github_provider",
    owner=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["owner"],
    token=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["token"],
)
stack_info = parse_stack()

ecommerce_config = Config("ecommerce")
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
ecommerce_environment = f"applications-{stack_info.env_suffix}"

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": ecommerce_environment},
)
vault_config = Config("vault")

consul_provider = get_consul_provider(stack_info)
setup_vault_provider(stack_info)

k8s_global_labels = K8sGlobalLabels(
    ou=BusinessUnit.ecommerce, service=Services.ecommerce, stack=stack_info
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

if not (UNIFIED_ECOMMERCE_DOCKER_TAG := os.getenv("UNIFIED_ECOMMERCE_DOCKER_TAG")):
    msg = "UNIFIED_ECOMMERCE_DOCKER_TAG must be set"
    raise OSError(msg)

match stack_info.env_suffix:
    case "production":
        env_var_suffix = "PROD"
    case "qa":
        env_var_suffix = "RC"
    case "ci":
        env_var_suffix = "CI"
    case _:
        env_var_suffix = "INVALID"


aws_account = get_caller_identity()

ecommerce_namespace = "ecommerce"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(ecommerce_namespace, ns)
)

ol_zone_id = dns_stack.require_output("ol")["id"]

# Frontend storage bucket
unified_ecommerce_app_storage_bucket_name = (
    f"ol-mit-unified-ecommerce-{stack_info.env_suffix}"
)
unified_ecommerce_app_storage_bucket = s3.Bucket(
    f"unified-ecommerce-app-storage-{stack_info.env_suffix}",
    bucket=unified_ecommerce_app_storage_bucket_name,
    tags=aws_config.tags,
)

s3.BucketVersioning(
    f"unified-ecommerce-app-storage-versioning-{stack_info.env_suffix}",
    bucket=unified_ecommerce_app_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled",
    ),
)
unified_ecommerce_app_storage_bucket_ownership_controls = s3.BucketOwnershipControls(
    f"unified-ecommerce-app-storage-ownership-controls-{stack_info.env_suffix}",
    bucket=unified_ecommerce_app_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)

unified_ecommerce_app_storage_bucket_public_access = s3.BucketPublicAccessBlock(
    f"unified-ecommerce-app-storage-public-access-{stack_info.env_suffix}",
    bucket=unified_ecommerce_app_storage_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)

unified_ecommerce_app_storage_bucket_policy = s3.BucketPolicy(
    f"unified-ecommerce-app-storage-policy-{stack_info.env_suffix}",
    bucket=unified_ecommerce_app_storage_bucket.id,
    policy=unified_ecommerce_app_storage_bucket.arn.apply(
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
            unified_ecommerce_app_storage_bucket_public_access,
            unified_ecommerce_app_storage_bucket_ownership_controls,
        ],
    ),
)

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3.putobjectacl"]}],
    },
    "RESOURCE_EFFECTIVLY_STAR": {},
}

gh_workflow_s3_bucket_permissions_doc = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Action": [
                "s3:ListBucket*",
            ],
            "Effect": "Allow",
            "Resource": [f"arn:aws:s3:::{unified_ecommerce_app_storage_bucket_name}"],
        },
        {
            "Action": [
                "s3:GetObject*",
                "s3:PutObject",
                "s3:PutObjectAcl",
                "s3:DeleteObject",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:s3:::{unified_ecommerce_app_storage_bucket_name}/frontend/*"
            ],
        },
    ],
}

gh_workflow_iam_policy = iam.Policy(
    f"unified-ecommerce-gh-workflow-iam-policy-{stack_info.env_suffix}",
    name=f"unified-ecommerce-gh-workflow-iam-policy-{stack_info.env_suffix}",
    policy=lint_iam_policy(
        gh_workflow_s3_bucket_permissions_doc,
        stringify=True,
        parliament_config=parliament_config,
    ),
)

# Just create a static user for now. Some day refactor to use
# https://github.com/hashicorp/vault-action
gh_workflow_user = iam.User(
    f"unified-ecommerce-gh-workflow-user-{stack_info.env_suffix}",
    name=f"ecommerce-gh-workflow-{stack_info.env_suffix}",
    tags=aws_config.tags,
)
iam.PolicyAttachment(
    f"unified-ecommerce-gh-workflow-iam-policy-attachment-{stack_info.env_suffix}",
    policy_arn=gh_workflow_iam_policy.arn,
    users=[gh_workflow_user.name],
)
gh_workflow_accesskey = iam.AccessKey(
    f"unified-ecommerce-gh-workflow-access-key-{stack_info.env_suffix}",
    user=gh_workflow_user.name,
    status="Active",
)
gh_repo = github.get_repository(
    full_name="mitodl/unified-ecommerce-frontend",
    opts=InvokeOptions(provider=github_provider),
)

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
        ".html",
        ".css",
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
unified_ecommerce_fastly_service = fastly.ServiceVcl(
    f"unified-ecommerce-fastly-service-{stack_info.env_suffix}",
    name=f"Unified Ecommerce {stack_info.env_suffix}",
    comment="Managed by Pulumi",
    backends=[
        fastly.ServiceVclBackendArgs(
            address=unified_ecommerce_app_storage_bucket.bucket_domain_name,
            name="unified-ecommerce frontend",
            override_host=unified_ecommerce_app_storage_bucket.bucket_domain_name,
            port=DEFAULT_HTTPS_PORT,
            ssl_cert_hostname=unified_ecommerce_app_storage_bucket.bucket_domain_name,
            ssl_sni_hostname=unified_ecommerce_app_storage_bucket.bucket_domain_name,
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
            name=ecommerce_config.require("frontend_domain"),
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
            name="Rewrite requests to root s3 - miss",
            content=textwrap.dedent(
                r"""
                if (req.method == "GET" && req.backend.is_origin) {
                  set bereq.url = "/frontend" + req.url;
                  if (req.url.path ~ "\/$" || req.url.basename !~ "\." ) {
                    set bereq.url = "/frontend/index.html";
                  }
                }
                """
            ),
            type="miss",
        ),
        fastly.ServiceVclSnippetArgs(
            name="Rewrite requests to root s3 - bypass",
            content=textwrap.dedent(
                r"""
                if (req.method == "GET" && req.backend.is_origin && req.http.User-Agent ~ "(?i)prerender") {
                  set req.backend = F_unified_ecommerce_frontend;
                  set bereq.url = "/frontend" + req.url;
                  if (req.url.path ~ "\/$" || req.url.basename !~ "\." ) {
                    set bereq.url = "/frontend/index.html";
                  }
                }
                """  # noqa: E501
            ),
            type="pass",
        ),
        fastly.ServiceVclSnippetArgs(
            name="Redirect for to correct domain",
            content=textwrap.dedent(
                rf"""
                # redirect to the correct host/domain
                if (obj.status == 618 && obj.response == "redirect-host") {{
                  set obj.status = 302;
                  set obj.http.Location = "https://" + "{ecommerce_config.require("frontend_domain")}" + req.url.path + if (std.strlen(req.url.qs) > 0, "?" req.url.qs, "");
                  return (deliver);
                }}
                """  # noqa: E501
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
    f"unified-ecommerce-frontend-dns-{stack_info.env_suffix}",
    name=ecommerce_config.require("frontend_domain"),
    allow_overwrite=True,
    type="A",
    ttl=five_minutes,
    records=[str(addr) for addr in FASTLY_A_TLS_1_3],
    zone_id=ol_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)

################################################
# Put the application secrets into vault
ecommerce_vault_secrets = read_yaml_secrets(
    Path(f"unified_ecommerce/secrets.{stack_info.env_suffix}.yaml"),
)
ecommerce_vault_mount = vault.Mount(
    f"unified-ecommerce-secrets-mount-{stack_info.env_suffix}",
    path="secret-ecommerce",
    type="kv-v2",
    options={"version": "2"},
    description="Secrets for the unified ecommerce application.",
    opts=ResourceOptions(delete_before_replace=True),
)
ecommerce_static_vault_secrets = vault.generic.Secret(
    f"unified-ecommerce-secrets-{stack_info.env_suffix}",
    path=ecommerce_vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps(ecommerce_vault_secrets),
)

# Application security group
ecommerce_application_security_group = ec2.SecurityGroup(
    f"unified-ecommerce-application-security-group-{stack_info.env_suffix}",
    name=f"unified-ecommerce-application-security-group-{stack_info.env_suffix}",
    description="Access control for the unified ecommerce application pods.",
    # allow all egress traffic
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(
        k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs,
    ),
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)


ecommerce_db_config: appdb.OLAppDatabaseConfig = appdb.OLAppDatabaseConfig(
    app_name="unified-ecommerce",
    app_security_group=ecommerce_application_security_group,
    app_db_name="ecommerce",
    app_vpc=apps_vpc,
    aws_config=aws_config,
    app_db_password=ecommerce_config.get("db_password"),
    alias_map={
        appdb.AliasKey.secgroup: [Alias(parent=ROOT_STACK_RESOURCE)],
        appdb.AliasKey.db: [Alias(parent=ROOT_STACK_RESOURCE)],
    },
)

ecommerce_db = appdb.OLAppDatabase(
    ol_db_config=ecommerce_db_config,
)

ecommerce_db_consul_node = Node(
    f"unified-ecommerce-{stack_info.env_suffix}-postgres-db",
    name="ecommerce-postgres-db",
    address=ecommerce_db.app_db.db_instance.address,
    opts=consul_provider,
)

ecommerce_db_consul_service = Service(
    "unified-ecommerce-{stack_info.env_suffix}-instance-db-consul-service",
    node=ecommerce_db_consul_node.name,
    name="ecommerce-db",
    port=ecommerce_db.app_db.db_instance.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="ecommerce-instance-db",
            interval="10s",
            name="ecommerce-instance-id",
            timeout="60s",
            status="passing",
            tcp=ecommerce_db.app_db.db_instance.address.apply(
                lambda address: f"{address}:{ecommerce_db.app_db.db_instance.port}"
            ),
        )
    ],
    opts=consul_provider,
)

# Redis configuration and networking setup
redis_config = Config("redis")
redis_instance_type = (
    redis_config.get("instance_type") or CacheInstanceTypes.micro.value
)

redis_cluster_security_group = ec2.SecurityGroup(
    f"unified-ecommerce-redis-cluster-security-group-{stack_info.env_suffix}",
    name_prefix=f"unified-ecommerce-{stack_info.env_suffix}-",
    description="Access control for the unified ecommerce redis cluster.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[ecommerce_application_security_group.id],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description="Allow application pods to talk to redis",
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
    instance_type=redis_instance_type,
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for unified-ecommerce tasks and caching.",
    cluster_name=f"unified-ecommerce-redis-{stack_info.env_suffix}",
    subnet_group=apps_vpc["elasticache_subnet"],
    security_groups=[redis_cluster_security_group.id],
    tags=aws_config.tags,
)
redis_cache = OLAmazonCache(
    redis_cache_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"unified-ecommerce-redis-{stack_info.env_suffix}-redis-elasticache-cluster"
            )
        ]
    ),
)

# Create a vault policy and associate it with an auth backend role
# on the vault k8s cluster auth endpoint
ecommerce_vault_policy = vault.Policy(
    f"unified-ecommerce-{stack_info.env_suffix}-vault-policy",
    name="unified-ecommerce",
    policy=Path(__file__).parent.joinpath("ecommerce_policy.hcl").read_text(),
)
ecommerce_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    "ecommerce-vault-k8s-auth-backend-role",
    role_name="unified-ecommerce",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[ecommerce_namespace],
    token_policies=[ecommerce_vault_policy.name],
)

vault_k8s_resources_config = OLVaultK8SResourcesConfig(
    application_name="unified-ecommerce",
    namespace=ecommerce_namespace,
    labels=k8s_global_labels,
    vault_address=vault_config.require("address"),
    vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
    vault_auth_role_name=ecommerce_vault_auth_backend_role.role_name,
)
vault_k8s_resources = OLVaultK8SResources(
    resource_config=vault_k8s_resources_config,
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[ecommerce_vault_auth_backend_role],
    ),
)

# Load the database creds into a k8s secret via VSO
db_creds_secret_name = "pgsql-db-creds"  # noqa: S105  # pragma: allowlist secret
db_creds_secret = Output.all(
    address=ecommerce_db.app_db.db_instance.address,
    port=ecommerce_db.app_db.db_instance.port,
    db_name=ecommerce_db.app_db.db_instance.db_name,
).apply(
    lambda db: OLVaultK8SSecret(
        f"unified-ecommerce-{stack_info.env_suffix}-db-creds-secret",
        OLVaultK8SDynamicSecretConfig(
            name="ecommerce-db-creds",
            namespace=ecommerce_namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=db_creds_secret_name,
            labels=k8s_global_labels,
            mount=ecommerce_db.app_db_vault_backend.db_mount.path,
            path="creds/app",
            restart_target_kind="Deployment",
            restart_target_name="ecommerce-app",
            templates={
                "DATABASE_URL": f'postgres://{{{{ get .Secrets "username"}}}}:{{{{ get .Secrets "password" }}}}@{db["address"]}:{db["port"]}/{db["db_name"]}',  # noqa: E501
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            parent=vault_k8s_resources,
            depends_on=[ecommerce_db],
        ),
    )
)

# Load the redis creds into a normal k8s secret
redis_creds_secret_name = "redis-creds"  # noqa: S105  # pragma: allowlist secret
redis_creds = kubernetes.core.v1.Secret(
    f"unified-ecommerce-{stack_info.env_suffix}-redis-creds",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=redis_creds_secret_name,
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
    ),
    string_data=redis_cache.address.apply(
        lambda address: {
            "CELERY_BROKER_URL": f"rediss://default:{redis_config.require('password')}@{address}:6379/0?ssl_cert_reqs=required",
            "CELERY_RESULT_BACKEND": f"rediss://default:{redis_config.require('password')}@{address}:6379/0?ssl_cert_reqs=required",
        }
    ),
    opts=ResourceOptions(
        depends_on=[redis_cache],
        delete_before_replace=True,
    ),
)

# Load the static secrets into a k8s secret via VSO
static_secrets_name = "ecommerce-static-secrets"  # pragma: allowlist secret
static_secrets = OLVaultK8SSecret(
    name=f"unified-ecommerce-{stack_info.env_suffix}-static-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="unified-ecommerce-static-secrets",
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
        dest_secret_name=static_secrets_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-ecommerce",
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
        depends_on=[ecommerce_static_vault_secrets],
    ),
)

# k8s Component Resource
secret_names = [db_creds_secret_name, redis_creds_secret_name, static_secrets_name]
ecommerce_k8s_config: OLApplicationK8sConfig = OLApplicationK8sConfig(
    project_root=Path(__file__).parent,
    application_config=ecommerce_config.require_object("env_vars"),
    application_name=Services.ecommerce,
    application_namespace=ecommerce_namespace,
    application_lb_service_name="ecommerce",
    application_lb_service_port_name="http",
    k8s_global_labels=k8s_global_labels,
    env_from_secret_names=secret_names,
    application_security_group_id=ecommerce_application_security_group.id,
    application_security_group_name=ecommerce_application_security_group.name,
    application_image_repository="mitodl/unified-ecommerce-app",
    application_docker_tag=UNIFIED_ECOMMERCE_DOCKER_TAG,
    vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
    import_nginx_config=True,
    probe_configs={},
)

ol_k8s_application = OLApplicationK8s(
    ol_app_k8s_config=ecommerce_k8s_config,
    opts=ResourceOptions(depends_on=ecommerce_db),
)


# Create the apisix custom resources since it doesn't support gateway-api yet

# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_plugin_config/
# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_pluginconfig_v2/
shared_plugin_config_name = "shared-plugin-config"
ecommerce_https_apisix_pluginconfig = kubernetes.apiextensions.CustomResource(
    f"unified-ecommerce-{stack_info.env_suffix}-https-apisix-pluginconfig",
    api_version="apisix.apache.org/v2",
    kind="ApisixPluginConfig",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=shared_plugin_config_name,
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "plugins": [
            {
                "name": "cors",
                "enable": True,
                "config": {
                    "allow_origins": "**",
                    "allow_methods": "**",
                    "allow_headers": "**",
                    "allow_credential": True,
                },
            },
            {
                "name": "response-rewrite",
                "enable": True,
                "config": {
                    "headers": {
                        "set": {
                            "Referrer-Policy": "origin",
                        },
                    },
                },
            },
        ],
    },
)

# Load open-id-connect secrets into a k8s secret via VSO
oidc_secret_name = "oidc-secrets"  # pragma: allowlist secret  # noqa: S105
oidc_secret = OLVaultK8SSecret(
    name=f"unified-ecommerce-{stack_info.env_suffix}-oidc-secrets",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="oidc-static-secrets",
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
        dest_secret_name=oidc_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/unified-ecommerce",
        excludes=[".*"],
        exclude_raw=True,
        templates={
            "client_id": '{{ get .Secrets "client_id" }}',
            "client_secret": '{{ get .Secrets "client_secret" }}',
            "realm": '{{ get .Secrets "realm_name" }}',
            "discovery": '{{ get .Secrets "url" }}/.well-known/openid-configuration',
        },
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
        depends_on=[ecommerce_static_vault_secrets],
    ),
)

mit_learn_oidc_secret_name = (
    "mit-learn-oidc-secrets"  # pragma: allowlist secret # noqa: S105
)
mit_learn_oidc_secret = OLVaultK8SSecret(
    f"ol-mitlearn-oidc-secrets-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="mit-learn-oidc-static-secrets",
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
        dest_secret_name=mit_learn_oidc_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount="secret-operations",
        mount_type="kv-v1",
        path="sso/mitlearn",
        excludes=[".*"],
        exclude_raw=True,
        # Refresh frequently because substructure keycloak stack could change these
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

ecommerce_api_domain = ecommerce_config.require("backend_domain")
learn_api_domain = ecommerce_config.require("learn_backend_domain")

# ApisixUpstream resources don't seem to work but we don't really need them?
# Ref: https://github.com/apache/apisix-ingress-controller/issues/1655
# Ref: https://github.com/apache/apisix-ingress-controller/issues/1855

# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_route_v2/
# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/
# LEGACY RETIREMENT: goes away
ecommerce_https_apisix_route = kubernetes.apiextensions.CustomResource(
    f"unified-ecommerce-{stack_info.env_suffix}-https-apisix-route",
    api_version="apisix.apache.org/v2",
    kind="ApisixRoute",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="ecommerce-https",
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "http": [
            {
                # unauthenticated routes, including assests and checkout callback API
                "name": "ue-unauth",
                "priority": 1,
                "match": {
                    "hosts": [
                        ecommerce_api_domain,
                    ],
                    "paths": [
                        "/api/*",
                        "/_/*",
                        "/logged_out/*",
                        "/auth/*",
                        "/static/*",
                        "/favicon.ico",
                        "/checkout/*",
                    ],
                },
                "plugin_config_name": shared_plugin_config_name,
                "backends": [
                    {
                        "serviceName": ol_k8s_application.application_lb_service_name,
                        "servicePort": ol_k8s_application.application_lb_service_port_name,  # noqa: E501
                    }
                ],
            },
            {
                # wildcard route for the rest of the system - auth required
                "name": "ue-default",
                "priority": 0,
                "plugins": [
                    # Ref: https://apisix.apache.org/docs/apisix/plugins/openid-connect/
                    {
                        "name": "openid-connect",
                        "enable": True,
                        # Get all the sensitive parts of this config from a secret
                        "secretRef": oidc_secret_name,
                        "config": {
                            "scope": "openid profile ol-profile",
                            "bearer_only": False,
                            "introspection_endpoint_auth_method": "client_secret_post",
                            "ssl_verify": False,
                            "logout_path": "/logout",
                            "discovery": "https://sso-qa.ol.mit.edu/realms/olapps/.well-known/openid-configuration",
                        },
                    },
                ],
                "plugin_config_name": shared_plugin_config_name,
                "match": {
                    "hosts": [
                        ecommerce_api_domain,
                    ],
                    "paths": [
                        "/cart/*",
                        "/admin/*",
                        "/establish_session/*",
                        "/logout",
                    ],
                },
                "backends": [
                    {
                        "serviceName": ol_k8s_application.application_lb_service_name,
                        "servicePort": ol_k8s_application.application_lb_service_port_name,  # noqa: E501
                    }
                ],
            },
            # Strip trailing slack from logout redirect
            {
                "name": "ue-logout-redirect",
                "priority": 0,
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
                    "hosts": [
                        ecommerce_api_domain,
                    ],
                    "paths": [
                        "/logout/*",
                    ],
                },
                "backends": [
                    {
                        "serviceName": ol_k8s_application.application_lb_service_name,
                        "servicePort": ol_k8s_application.application_lb_service_port_name,  # noqa: E501
                    }
                ],
            },
        ]
    },
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[ol_k8s_application],
    ),
)

proxy_rewrite_plugin_config = {
    "name": "proxy-rewrite",
    "enable": True,
    "config": {
        "regex_uri": [
            "/commerce/(.*)",
            "/$1",
        ],
    },
}

# New ApisixRoute object for the learn.mit.edu address
# All paths prefixed with /commerce
# Host match is only the mit-learn domain
mit_learn_ecommerce_https_apisix_route = kubernetes.apiextensions.CustomResource(
    f"mit-learn-unified-ecommerce-{stack_info.env_suffix}-https-apisix-route",
    api_version="apisix.apache.org/v2",
    kind="ApisixRoute",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mit-learn-ecommerce-https",
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "http": [
            {
                # unauthenticated routes, including assests and checkout callback API
                "name": "ue-unauth",
                "priority": 100,
                "plugins": [proxy_rewrite_plugin_config],
                "match": {
                    "hosts": [
                        learn_api_domain,
                    ],
                    "paths": [
                        "/commerce/api/*",
                        "/commerce/_/*",
                        "/commerce/logged_out/*",
                        "/commerce/auth/*",
                        "/commerce/static/*",
                        "/commerce/favicon.ico",
                        "/commerce/checkout/*",
                    ],
                },
                "plugin_config_name": shared_plugin_config_name,
                "backends": [
                    {
                        "serviceName": ol_k8s_application.application_lb_service_name,
                        "servicePort": ol_k8s_application.application_lb_service_port_name,  # noqa: E501
                    }
                ],
            },
            {
                # wildcard route for the rest of the system - auth required
                "name": "ue-default",
                "priority": 100,
                "plugins": [
                    proxy_rewrite_plugin_config,
                    # Ref: https://apisix.apache.org/docs/apisix/plugins/openid-connect/
                    {
                        "name": "openid-connect",
                        "enable": True,
                        # Get all the sensitive parts of this config from a secret
                        "secretRef": mit_learn_oidc_secret_name,
                        "config": {
                            "scope": "openid profile ol-profile",
                            "bearer_only": False,
                            "introspection_endpoint_auth_method": "client_secret_post",
                            "ssl_verify": False,
                            "logout_path": "/logout",
                            "discovery": "https://sso-qa.ol.mit.edu/realms/olapps/.well-known/openid-configuration",
                        },
                    },
                ],
                "plugin_config_name": shared_plugin_config_name,
                "match": {
                    "hosts": [
                        learn_api_domain,
                    ],
                    "paths": [
                        "/commerce/cart/*",
                        "/commerce/admin/*",
                        "/commerce/establish_session/*",
                        "/commerce/logout",
                    ],
                },
                "backends": [
                    {
                        "serviceName": ol_k8s_application.application_lb_service_name,
                        "servicePort": ol_k8s_application.application_lb_service_port_name,  # noqa: E501
                    }
                ],
            },
            # Strip trailing slack from logout redirect
            {
                "name": "ue-logout-redirect",
                "priority": 100,
                "plugins": [
                    proxy_rewrite_plugin_config,
                    {
                        "name": "redirect",
                        "enable": True,
                        "config": {
                            "uri": "/commerce/logout",
                        },
                    },
                ],
                "match": {
                    "hosts": [
                        learn_api_domain,
                    ],
                    "paths": [
                        "/commerce/logout/*",
                    ],
                },
                "backends": [
                    {
                        "serviceName": ol_k8s_application.application_lb_service_name,
                        "servicePort": ol_k8s_application.application_lb_service_port_name,  # noqa: E501
                    }
                ],
            },
        ]
    },
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[ol_k8s_application],
    ),
)

# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_tls_v2/
# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
# LEGACY RETIREMENT : goes away
# Won't need this because it will exist from the mit-learn namespace
ecommerce_https_apisix_tls = kubernetes.apiextensions.CustomResource(
    f"unified-ecommerce-{stack_info.env_suffix}-https-apisix-tls",
    api_version="apisix.apache.org/v2",
    kind="ApisixTls",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="ecommerce-https",
        namespace=ecommerce_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "hosts": [ecommerce_config.require("backend_domain")],
        # Use the shared ol-wildcard cert loaded into every cluster
        "secret": {
            "name": "ol-wildcard-cert",
            "namespace": "operations",
        },
    },
)


# Finally, put the aws access key into the github actions configuration
gh_workflow_access_key_id_env_secret = github.ActionsSecret(
    f"unified-ecommerce-gh-workflow-access-key-id-env-secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"AWS_ACCESS_KEY_ID_{env_var_suffix}",  # pragma: allowlist secret
    plaintext_value=gh_workflow_accesskey.id,
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_secretaccesskey_env_secret = github.ActionsSecret(
    f"unified-ecommerce-gh-workflow-secretaccesskey-env-secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"AWS_SECRET_ACCESS_KEY_{env_var_suffix}",  # pragma: allowlist secret
    plaintext_value=gh_workflow_accesskey.secret,
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)

gh_workflow_fastly_api_key_env_secret = github.ActionsSecret(
    f"unified-ecommerce-gh-workflow-fastly-api-key-env-secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"FASTLY_API_KEY_{env_var_suffix}",  # pragma: allowlist secret
    plaintext_value=ecommerce_config.require("fastly_api_key"),
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)
gh_workflow_fastly_service_id_env_secret = github.ActionsSecret(
    f"unified-ecommerce-gh-workflow-fastly-service-id-env-secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"FASTLY_SERVICE_ID_{env_var_suffix}",  # pragma: allowlist secret
    plaintext_value=unified_ecommerce_fastly_service.id,
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)

gh_workflow_s3_bucket_name_env_secret = github.ActionsVariable(
    f"unified-ecommerce-gh-workflow-s3-bucket-name-env-variable-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"AWS_S3_BUCKET_NAME_{env_var_suffix}",  # pragma: allowlist secret
    value=unified_ecommerce_app_storage_bucket_name,
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)

gh_workflow_api_base_env_var = github.ActionsVariable(
    f"unified-ecommerce-gh-workflow-api-base-env-variable-{stack_info.env_suffix}",
    repository=gh_repo.name,
    variable_name=f"API_BASE_{env_var_suffix}",  # pragma: allowlist secret
    value=f"https://{learn_api_domain}/commerce",
    opts=ResourceOptions(provider=github_provider, delete_before_replace=True),
)

# No route53 config for ecommerce_config.require("backend_domain") because
# the external-dns service in the cluster will take care of it for us.
