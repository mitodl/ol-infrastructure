import json
import os
from pathlib import Path

import pulumi_fastly as fastly
import pulumi_github as github
import pulumi_vault as vault
from pulumi import Config, InvokeOptions, Output, ResourceOptions, StackReference
from pulumi_aws import ec2, get_caller_identity, iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_3
from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.aws.rds_helper import DBInstanceTypes
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

aws_account = get_caller_identity

stack_info = parse_stack()

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
monitoring_stack = StackReference("infrastructure.monitoring")
network_stack = StackReference(f"infrastructure.aws.networking.{stack_info.name}")
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
    "ol.mit.edu/service": "learn-ai",
}
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Fail hard if LEARN_AI_DOCKER_TAG is not set
if "LEARN_AI_DOCKER_TAG" not in os.environ:
    msg = "LEARN_AI_DOCKER_TAG must be set"
    raise OSError(msg)
LEARN_AI_DOCKER_TAG = os.getenv("LEARN_AI_DOCKER_TAG")

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
}

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
                  set req.backend = F_learn_ai_frontend;
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
                  set obj.http.Location = "https://" + "{learn_ai_config.require("frontend_domain")}" + req.url.path + if (std.strlen(req.url.qs) > 0, "?" req.url.qs, "");
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
    Path(f"unified_learn_ai/secrets.{stack_info.env_suffix}.yaml"),
)
learn_ai_vault_mount = vault.Mount(
    f"unified-learn-ai-secrets-mount-{stack_info.env_suffix}",
    path="secret-learn-ai",
    type="kv-v2",
    options={"version": "2"},
    description="Secrets for the unified learn_ai application.",
    opts=ResourceOptions(delete_before_replace=True),
)
learn_ai_static_vault_secrets = vault.generic.Secret(
    f"unified-learn-ai-secrets-{stack_info.env_suffix}",
    path=learn_ai_vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps(learn_ai_vault_secrets),
)

################################################
# Application security group
# Needs to happen ebfore the database security group is created
learn_ai_application_security_group = ec2.SecurityGroup(
    f"unified-learn-ai-application-security-group-{stack_info.env_suffix}",
    name=f"unified-learn-ai-application-security-group-{stack_info.env_suffix}",
    description="Access control for the unified learn-ai application pods.",
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
learn_ai_database_security_group = ec2.SecurityGroup(
    f"unified-learn-ai-db-security-group-{stack_info.env_suffix}",
    name=f"unified-learn-ai-db-security-group-{stack_info.env_suffix}",
    description="Access control for the unified learn-ai database.",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                consul_security_groups["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to postgres from consul and vault.",
        ),
        ec2.SecurityGroupIngressArgs(
            security_groups=[learn_ai_application_security_group.id],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Allow application pods to talk to DB",
        ),
    ],
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    learn_ai_config.get("db_instance_size") or DBInstanceTypes.small.value
)

learn_ai_db_config = OLPostgresDBConfig(
    instance_name=f"unified-learn-ai-db-{stack_info.env_suffix}",
    password=learn_ai_config.get("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[learn_ai_database_security_group],
    storage=learn_ai_config.get("db_capacity")
    or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    engine_major_version="16",
    tags=aws_config.tags,
    db_name="learn-ai",
    **defaults(stack_info)["rds"],
)
learn_ai_db = OLAmazonDB(learn_ai_db_config)

learn_ai_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=learn_ai_db_config.db_name,
    mount_point=f"{learn_ai_db_config.engine}-learn-ai",
    db_admin_username=learn_ai_db_config.username,
    db_admin_password=learn_ai_config.get("db_password"),
    db_host=learn_ai_db.db_instance.address,
)
learn_ai_db_vault_backend = OLVaultDatabaseBackend(
    learn_ai_db_vault_backend_config,
    opts=ResourceOptions(delete_before_replace=True, parent=learn_ai_db),
)
