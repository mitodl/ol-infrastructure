# ruff: noqa: E501
"""Learn AI application infrastructure deployment (Pulumi)."""

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
)
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_REDIS_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.learn_ai import definition
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services import appdb
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
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sCeleryRedisConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
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
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.azure_workload_identity import (
    azure_identity_env,
    azure_identity_token_mount,
    azure_identity_token_volume,
    azure_openai_env,
)
from ol_infrastructure.lib.fastly import (
    get_fastly_provider,
    s3_sigv4_signing_vcl,
    vcl_snippet,
)
from ol_infrastructure.lib.fastly_logging import fastly_logging_args
from ol_infrastructure.lib.k8s_keda import (
    build_webapp_keda_config,
    create_webapp_prometheus_trigger_auth,
)
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.opik import (
    OPIK_KEYCLOAK_SECRET_TEMPLATES,
    OPIK_SDK_VAULT_MOUNT,
    opik_api_url,
    opik_sdk_vault_path,
)
from ol_infrastructure.lib.pulumi_helper import (
    docker_image_config_kwargs,
    make_stack_reference,
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
network_stack = make_stack_reference(projects.NETWORKING, stack_info.name)
policy_stack = make_stack_reference(projects.POLICIES, "default")
sentry_stack = make_stack_reference(projects.SENTRY, "default")
vault_stack = make_stack_reference(
    projects.VAULT_SERVER, f"operations.{stack_info.name}"
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

setup_vault_provider(stack_info)
fastly_provider = get_fastly_provider()

k8s_global_labels = K8sGlobalLabels(
    ou=BusinessUnit.mit_learn, service=Services.mit_learn, stack=stack_info
).model_dump()
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

learn_ai_namespace = definition.NAMESPACE
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(learn_ai_namespace, ns)
)

ol_zone_id = dns_stack.require_output("ol")["id"]

################################################
# Frontend storage bucket
learn_ai_app_storage_bucket_name = f"ol-mit-learn-ai-{stack_info.env_suffix}"

# Every environment uses SigV4-signed Fastly->S3 requests, so the bucket can be
# fully private instead of public-read (hq#13287 item 1; mit-learn runs the same
# pattern). Kept as a flag so production can be rolled back by dropping it here.
_learn_ai_bucket_is_sigv4_piloted = stack_info.env_suffix in {"ci", "qa", "production"}

learn_ai_app_storage_bucket_config = S3BucketConfig(
    bucket_name=learn_ai_app_storage_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    block_public_acls=_learn_ai_bucket_is_sigv4_piloted,
    block_public_policy=_learn_ai_bucket_is_sigv4_piloted,
    ignore_public_acls=_learn_ai_bucket_is_sigv4_piloted,
    restrict_public_buckets=_learn_ai_bucket_is_sigv4_piloted,
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

learn_ai_fastly_s3_signer_access_key = None
if _learn_ai_bucket_is_sigv4_piloted:
    # Dedicated identity for Fastly to authenticate to S3 with (SigV4), scoped to
    # GetObject on just this bucket. It must live under /ol-applications/ and carry
    # no tags: Concourse's deploy role only manages IAM users under that path, via
    # managed-policy attach, and has no iam:TagUser (see the same note on the
    # mit-learn signer).
    learn_ai_fastly_s3_signer_user = iam.User(
        f"ol-learn-ai-fastly-s3-signer-{stack_info.env_suffix}",
        name=f"ol-learn-ai-{stack_info.env_suffix}-fastly-s3-signer",
        path="/ol-applications/",
    )
    learn_ai_fastly_s3_signer_policy = iam.Policy(
        f"ol-learn-ai-fastly-s3-signer-policy-{stack_info.env_suffix}",
        path="/ol-applications/",
        policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject"],
                        "Resource": [
                            f"arn:aws:s3:::{learn_ai_app_storage_bucket_name}/*"
                        ],
                    }
                ],
            }
        ),
    )
    learn_ai_fastly_s3_signer_policy_attachment = iam.UserPolicyAttachment(
        f"ol-learn-ai-fastly-s3-signer-policy-attachment-{stack_info.env_suffix}",
        user=learn_ai_fastly_s3_signer_user.name,
        policy_arn=learn_ai_fastly_s3_signer_policy.arn,
    )
    learn_ai_fastly_s3_signer_access_key = iam.AccessKey(
        f"ol-learn-ai-fastly-s3-signer-access-key-{stack_info.env_suffix}",
        user=learn_ai_fastly_s3_signer_user.name,
        opts=ResourceOptions(depends_on=[learn_ai_fastly_s3_signer_policy_attachment]),
    )
    # The durable Vault copy of this key is created further down -- search for
    # "fastly-s3-signer-vault-secret".
    learn_ai_bucket_policy_document = Output.all(
        arn=learn_ai_app_storage_bucket.bucket_v2.arn,
        signer_arn=learn_ai_fastly_s3_signer_user.arn,
    ).apply(
        lambda args: json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "FastlySigV4Read",
                        "Effect": "Allow",
                        "Principal": {"AWS": args["signer_arn"]},
                        "Action": "s3:GetObject",
                        "Resource": f"{args['arn']}/*",
                    }
                ],
            }
        )
    )
else:
    learn_ai_bucket_policy_document = learn_ai_app_storage_bucket.bucket_v2.arn.apply(
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
    )

learn_ai_app_storage_bucket_policy = s3.BucketPolicy(
    f"learn-ai-app-storage-bucket-policy-{stack_info.env_suffix}",
    bucket=learn_ai_app_storage_bucket.bucket_v2.id,
    policy=learn_ai_bucket_policy_document,
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

learn_ai_frontend_domain = learn_ai_config.require("frontend_domain")
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
learn_ai_backend_name = "learn-ai"

learn_ai_s3_auth_snippets = []
if _learn_ai_bucket_is_sigv4_piloted and learn_ai_fastly_s3_signer_access_key:
    learn_ai_s3_backend_auth_vcl = Output.all(
        access_key_id=learn_ai_fastly_s3_signer_access_key.id,
        secret_access_key=learn_ai_fastly_s3_signer_access_key.secret,
    ).apply(
        lambda args: s3_sigv4_signing_vcl(
            args["access_key_id"],
            args["secret_access_key"],
            f"{learn_ai_app_storage_bucket_name}.s3.amazonaws.com",
            learn_ai_backend_name,
        )
    )
    learn_ai_s3_auth_snippets = [
        vcl_snippet(
            name="Sign S3 miss requests with SigV4",
            content=learn_ai_s3_backend_auth_vcl,
            type="miss",
        ),
        vcl_snippet(
            name="Sign S3 pass requests with SigV4",
            content=learn_ai_s3_backend_auth_vcl,
            type="pass",
        ),
    ]

learn_ai_fastly_service = fastly.ServiceVcl(
    f"learn-ai-fastly-service-{stack_info.env_suffix}",
    name=f"Learn AI {stack_info.env_suffix}",
    comment="Managed by Pulumi",
    backends=[
        fastly.ServiceVclBackendArgs(
            address=learn_ai_app_storage_bucket.bucket_v2.bucket_domain_name,
            name=learn_ai_backend_name,
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
            extensions=sorted(gzip_settings["extensions"]),
            content_types=sorted(gzip_settings["content_types"]),
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
        vcl_snippet(
            name="Add frontend to path",
            content=Path("files/frontend_path_prefix.vcl").read_text(),
            type="recv",
        ),
        vcl_snippet(
            name="Return custom 404 page",
            content=Path("files/custom_404.vcl").read_text(),
            type="deliver",
        ),
        vcl_snippet(
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
        *learn_ai_s3_auth_snippets,
    ],
    **fastly_logging_args(
        name=f"fastly-learn_ai-{stack_info.env_suffix}",
        application=Application.learn_ai,
        environment=stack_info.env_suffix,
        s3_path=f"/{Application.learn_ai}/{stack_info.env_suffix}/",
    ),
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
if _learn_ai_bucket_is_sigv4_piloted and learn_ai_fastly_s3_signer_access_key:
    # Durable copy in Vault for visibility/rotation tooling outside of Pulumi state.
    # Pulumi (via the iam.AccessKey resource) is still the source of truth --
    # rotating means replacing that resource, which updates this secret in the
    # same apply.
    # Deliberately NOT under secret-learn-ai: learn_ai_policy.hcl grants read on
    # secret-learn-ai/*, and the Kubernetes auth role accepts every service account
    # in the namespace, so any learn-ai workload could read the key. The app does
    # not need it (only Fastly does), and the policy lists specific
    # secret-operations paths, so a secret here is unreadable to the app role.
    vault.generic.Secret(
        f"learn-ai-fastly-s3-signer-vault-secret-{stack_info.env_suffix}",
        path="secret-operations/learn-ai/fastly-s3-signer",
        data_json=Output.all(
            access_key_id=learn_ai_fastly_s3_signer_access_key.id,
            secret_access_key=learn_ai_fastly_s3_signer_access_key.secret,
        ).apply(json.dumps),
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

# Opik LLM-observability credentials. Every environment traces to the Production
# Opik (lib/opik.py), so this is learn-ai's Production Keycloak client for this
# environment, ``opik-learn-ai-<env>`` (client-credentials grant), which the
# Production keycloak substructure writes to ``secret-opik/sdk-learn-ai`` in this
# environment's Vault. Sync it into a
# K8s secret via VSO and expose it to the app as the OPIK_KEYCLOAK_* env vars.
opik_keycloak_secret_name = "learn-ai-opik-keycloak"  # pragma: allowlist secret  # noqa: S105
opik_keycloak_secret = OLVaultK8SSecret(
    name=f"learn-ai-{stack_info.env_suffix}-opik-keycloak-secret",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="learn-ai-opik-keycloak",
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
        dest_secret_name=opik_keycloak_secret_name,
        dest_secret_labels=k8s_global_labels,
        mount=OPIK_SDK_VAULT_MOUNT,
        mount_type="kv-v1",
        path=opik_sdk_vault_path("learn-ai"),
        restart_targets=[{"kind": "Deployment", "name": "learn-ai-app"}],
        templates=OPIK_KEYCLOAK_SECRET_TEMPLATES,
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        parent=vault_k8s_resources,
    ),
)

env_vars = dict(learn_ai_config.require_object("env_vars") or {})

# Azure OpenAI, additive alongside the existing OPENAI_API_KEY wiring, which is not
# touched. Nothing here is secret: the managed identity is reached by exchanging the
# projected ServiceAccount token mounted below, so the client id is an identifier
# rather than a credential and there is nothing to rotate.
#
# A StackReference to a stack that does not exist fails the whole preview, so this is
# a config switch that gets flipped per environment once infrastructure/azure/openai
# has been deployed there.
if learn_ai_config.get_bool("enable_azure_openai"):
    azure_openai_stack = make_stack_reference(projects.AZURE_OPENAI, stack_info.name)
    env_vars.update(azure_identity_env(azure_openai_stack, "learn-ai"))
    env_vars.update(
        azure_openai_env(
            azure_openai_stack,
            "learn-ai",
            api_version=learn_ai_config.get("azure_openai_api_version") or "v1",
            default_deployment=learn_ai_config.get("azure_openai_default_deployment")
            or "gpt-4o",
        )
    )
    azure_identity_volumes = [azure_identity_token_volume()]
    azure_identity_volume_mounts = [azure_identity_token_mount()]
else:
    azure_identity_volumes = []
    azure_identity_volume_mounts = []

# Opik instrumentation (non-secret settings). OPIK_URL_OVERRIDE is the Production
# Opik's API for every environment; the workspace/project are static for our OSS
# install. The Opik SDK stamps OPIK_ENVIRONMENT on every trace and span as Opik's
# environment, which is what tells the environments apart in the shared learn-ai
# project. It is the stack's environment, so QA reports "qa" as mit-learn and
# dagster do, not MITOL_ENVIRONMENT's "rc". The Keycloak client id/secret and token
# URL arrive via envFrom (see the VSO secret above). Do NOT set OPIK_API_KEY: the
# app's Keycloak auth flow owns the Authorization header (see the opik stack's
# OPIK_SDK_KEYCLOAK_AUTH.md).
env_vars.update(
    {
        "OPIK_URL_OVERRIDE": opik_api_url(),
        "OPIK_WORKSPACE": "default",
        "OPIK_PROJECT_NAME": "learn-ai",
        "OPIK_ENVIRONMENT": stack_info.env_suffix,
    }
)

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
    # No latency trigger by default: about half of learn-ai's requests are LLM
    # agent calls (/ai/http/*_agent/) that take 2-10s, so p95 sat above 4.4s in
    # every hourly sample of the week to 2026-10-28 and a 2000ms trigger held
    # the production webapp at max replicas for 60+ days at ~0.25 req/s. Adding
    # replicas cannot shorten an LLM response. Set
    # `learn_ai:autoscaling_latency_threshold` on a stack to opt back in.
    latency_threshold=learn_ai_config.get("autoscaling_latency_threshold"),
    cpu_threshold=learn_ai_config.get("autoscaling_cpu_threshold") or "60",
)

learn_ai_api_domain = learn_ai_config.require("backend_domain")
learn_api_domain = learn_ai_config.require("learn_backend_domain")
_min_replicas = learn_ai_config.get_int("min_replicas")
min_replicas = 2 if _min_replicas is None else _min_replicas

bindings = definition.LearnAiBindings(
    env_suffix=stack_info.env_suffix,
    hostnames=definition.LearnAiHostnames(
        api=learn_ai_api_domain,
        frontend=learn_ai_frontend_domain,
        learn_api=learn_api_domain,
    ),
    environment_variables=env_vars,
    cors_frontend_origins=learn_ai_config.require_object("cors_frontend_origins"),
    k8s_labels=k8s_global_labels,
    secret_names=[
        db_creds_secret_name,
        redis_creds_secret_name,
        static_secrets_name,
    ],
    # Optional so pods start before the Production keycloak stack writes it.
    optional_secret_names=[opik_keycloak_secret_name],
    **docker_image_config_kwargs("LEARN_AI"),
    cluster=definition.ClusterCapabilities(
        security_group_id=learn_ai_application_security_group.id,
        # Use the fixed name used in the SecurityGroupPolicy spec
        security_group_name=Output.from_input("learn-ai-app"),
        vault_auth_name=vault_k8s_resources.auth_name,
    ),
    service_account_name=learn_ai_service_account.metadata.name,
    min_replicas=min_replicas,
    slack_channel=slack_channel,
    celery_redis=OLApplicationK8sCeleryRedisConfig(
        host=redis_cache.address,
        password=redis_config.require("password"),
    ),
    webapp_keda=learn_ai_webapp_keda_config,
    extra_volumes=azure_identity_volumes,
    extra_volume_mounts=azure_identity_volume_mounts,
)

learn_ai_app_k8s = OLApplicationK8s(
    ol_app_k8s_config=definition.application_config(bindings),
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
        cors_allow_origins=definition.cors_allow_origins(bindings),
        plugins=definition.shared_plugins(bindings),
    ),
    opts=ResourceOptions(delete_before_replace=True),
)

learn_ai_oidc_resources = OLApisixOIDCResources(
    f"learn-ai-{stack_info.env_suffix}-oidc-resources",
    oidc_config=definition.direct_oidc_config(bindings),
    opts=ResourceOptions(delete_before_replace=True, parent=vault_k8s_resources),
)

learn_ai_mit_learn_oidc_resources = OLApisixOIDCResources(
    f"learn-ai-mit-learn-{stack_info.env_suffix}-oidc-resources",
    oidc_config=definition.prefixed_oidc_config(bindings),
    opts=ResourceOptions(delete_before_replace=True, parent=vault_k8s_resources),
)

# ApisixUpstream resources don't seem to work but we don't really need them?
# Ref: https://github.com/apache/apisix-ingress-controller/issues/1655
# Ref: https://github.com/apache/apisix-ingress-controller/issues/1855

# Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_route_v2/
# Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/

# Instantiate ApisixRoute component for the learn.mit.edu address
mit_learn_learn_ai_https_apisix_route = OLApisixRoute(
    f"mit-learn-learn-ai-{stack_info.env_suffix}-https-olapisixroute",
    k8s_namespace=learn_ai_namespace,
    k8s_labels=k8s_global_labels,
    route_configs=definition.prefixed_route_configs(
        bindings,
        learn_ai_mit_learn_oidc_resources,
        shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
    ),
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
    route_configs=definition.direct_route_configs(
        bindings,
        learn_ai_oidc_resources,
        shared_plugin_config_name=learn_ai_shared_plugins.resource_name,
    ),
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
        name=definition.CANVAS_AGENT_CONSUMER_NAME,
        namespace=learn_ai_namespace,
        labels=k8s_global_labels,
    ),
    spec=definition.canvas_agent_consumer_spec(
        Output.secret(
            read_yaml_secrets(
                Path(f"vault/secrets.{stack_info.env_suffix}.yaml"),
            )["learn_ai"]["canvas_syllabus_token"]
        )
    ),
)

export(
    "learn_ai",
    {
        "rds_host": learn_ai_db.app_db.db_instance.address,
        "redis": redis_cache.address,
        "redis_token": redis_cache.cache_cluster.auth_token,
    },
)
