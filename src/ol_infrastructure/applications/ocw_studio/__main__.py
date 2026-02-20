# ruff: noqa: E501
"""Create the infrastructure and services needed to support the OCW Studio application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
- Optionally deploy the application to Kubernetes (toggle via ocw_studio:k8s_deploy)
"""

import json
import os
from pathlib import Path

import pulumi_github as github
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    InvokeOptions,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.magic_numbers import (
    DEFAULT_NGINX_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.applications.ocw_studio.k8s_secrets import (
    create_ocw_studio_k8s_secrets,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.mediaconvert import (
    MediaConvertConfig,
    OLMediaConvert,
)
from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.services.apisix_gateway_api import (
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sCeleryWorkerConfig,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import (
    Application,
    AWSBase,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider(skip_child_token=True)

ocw_studio_config = Config("ocw_studio")
stack_info = parse_stack()
k8s_deploy = ocw_studio_config.get_bool("k8s_deploy") or False

github_provider = github.Provider(
    "github-provider",
    owner=read_yaml_secrets(Path(f"pulumi/github_provider.yaml"))["owner"],  # noqa: F541
    token=read_yaml_secrets(Path(f"pulumi/github_provider.yaml"))["token"],  # noqa: F541
)
github_options = ResourceOptions(provider=github_provider)

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
data_vpc = network_stack.require_output("data_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
aws_config = AWSBase(
    tags={
        "OU": "open-courseware",
        "Environment": f"applications_{stack_info.env_suffix}",
        "Application": "ocw_studio",
    }
)

setup_heroku_provider()
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")

# AWS Account Information
aws_account = get_caller_identity()

# Create S3 buckets

# Bucket used to store file uploads from ocw-studio app.
ocw_storage_bucket_name = f"ol-ocw-studio-app-{stack_info.env_suffix}"

ocw_storage_bucket_config = S3BucketConfig(
    bucket_name=ocw_storage_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerPreferred",
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
    restrict_public_buckets=False,
    tags=aws_config.tags,
    bucket_policy_document=json.dumps(
        {
            "Version": "2008-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"AWS": "*"},
                    "Action": "s3:GetObject",
                    "Resource": f"arn:aws:s3:::{ocw_storage_bucket_name}/courses/*",
                }
            ],
        }
    ),
)

ocw_storage_bucket = OLBucket(
    "ocw-studio-app-bucket",
    config=ocw_storage_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"ol-ocw-studio-app-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-ocw-studio-app-ownership-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-ocw-studio-app-bucket-versioning",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-ocw-studio-app-bucket-public-access-controls",
                parent=ROOT_STACK_RESOURCE,
            ),
            Alias(
                name="ol-ocw-studio-app-bucket-policy",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

# Get the standard MediaConvert policy statements
mediaconvert_policy_statements = OLMediaConvert.get_standard_policy_statements(
    stack_info.env_suffix,
    service_name="ocw-studio",
)

ocw_studio_iam_policy = iam.Policy(
    f"ocw-studio-{stack_info.env_suffix}-policy",
    description=(
        "AWS access controls for the OCW Studio application in the "
        f"{stack_info.name} environment"
    ),
    path=f"/ol-applications/ocw-studio/{stack_info.env_suffix}/",
    name_prefix="aws-permissions-",
    policy=lint_iam_policy(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": "s3:ListAllMyBuckets",
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:*MultiPartUpload*",
                        "s3:ListBucket*",
                        "s3:PutObject*",
                        "s3:GetObject*",
                        "s3:DeleteObject*",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{ocw_storage_bucket_name}",
                        f"arn:aws:s3:::{ocw_storage_bucket_name}/*",
                    ],
                },
                {
                    "Effect": "Allow",
                    "Action": ["execute-api:Invoke", "execute-api:ManageConnections"],
                    "Resource": "arn:aws:execute-api:*:*:*",
                },
                # Temporary permissions until video archives are synced via management
                # command. TMM 2023-04-19
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:*MultiPartUpload*",
                        "s3:DeleteObject",
                        "s3:GetObject*",
                        "s3:ListBucket*",
                        "s3:PutObject*",
                        "s3:PutObjectTagging",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::ocw-content*{stack_info.env_suffix}",
                        f"arn:aws:s3:::ocw-content*{stack_info.env_suffix}/*",
                    ],
                },
                # Include standard MediaConvert policy statements
                *mediaconvert_policy_statements,
            ],
        },
        stringify=True,
        parliament_config={
            "PERMISSIONS_MANAGEMENT_ACTIONS": {
                "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
            },
            "RESOURCE_EFFECTIVELY_STAR": {},
        },
    ),
)


ocw_studio_vault_backend_role = vault.aws.SecretBackendRole(
    f"ocw-studio-app-{stack_info.env_suffix}",
    name=f"ocw-studio-app-{stack_info.env_suffix}",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "operations", "vault_managed": "True"},
    policy_arns=[ocw_studio_iam_policy.arn],
)

# Create RDS instance
db_ingress_rules = [
    ec2.SecurityGroupIngressArgs(
        protocol="tcp",
        from_port=DEFAULT_POSTGRES_PORT,
        to_port=DEFAULT_POSTGRES_PORT,
        security_groups=[data_vpc["security_groups"]["integrator"]],
        cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"],
    ),
    ec2.SecurityGroupIngressArgs(
        protocol="tcp",
        from_port=DEFAULT_POSTGRES_PORT,
        to_port=DEFAULT_POSTGRES_PORT,
        cidr_blocks=["0.0.0.0/0"],
        ipv6_cidr_blocks=["::/0"],
        description="Allow access over the public internet from Heroku",
    ),
]
if k8s_deploy:
    db_ingress_rules.append(
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=apps_vpc["k8s_pod_subnet_cidrs"],
            description="Allow access from the app running in Kubernetes",
        ),
    )

ocw_studio_db_security_group = ec2.SecurityGroup(
    f"ocw-studio-db-access-{stack_info.env_suffix}",
    description=f"Access control for the OCW Studio DB in {stack_info.name}",
    ingress=db_ingress_rules,
    egress=[
        ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    tags=aws_config.merged_tags(
        {"name": "ocw-studio-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=apps_vpc["id"],
)
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["use_blue_green"] = ocw_studio_config.get("db_use_blue_green") or False
ocw_studio_db_config = OLPostgresDBConfig(
    instance_name=f"ocw-studio-db-applications-{stack_info.env_suffix}",
    password=ocw_studio_config.require("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[ocw_studio_db_security_group],
    engine_major_version="18",
    tags=aws_config.tags,
    db_name="ocw_studio",
    public_access=True,
    **rds_defaults,
)
ocw_studio_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
)
ocw_studio_db = OLAmazonDB(ocw_studio_db_config)

ocw_studio_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=ocw_studio_db_config.db_name,
    mount_point=(
        f"{ocw_studio_db_config.engine}-ocw-studio-applications-{stack_info.env_suffix}"
    ),
    db_admin_username=ocw_studio_db_config.username,
    db_admin_password=ocw_studio_db_config.password.get_secret_value(),
    db_host=ocw_studio_db.db_instance.address,
    **rds_defaults,
)
ocw_studio_vault_backend = OLVaultDatabaseBackend(ocw_studio_vault_backend_config)

######################
# Secrets Management #
######################
ocw_studio_secrets = vault.Mount(
    "ocw-studio-vault-secrets-storage",
    path="secret-ocw-studio",
    type="kv-v2",
    description="Static secrets storage for the OCW Studio application",
)
vault_secrets = read_yaml_secrets(
    Path(f"ocw_studio/ocw_studio.{stack_info.env_suffix}.yaml")
)["collected_secrets"]
vault.generic.Secret(
    "ocw-studio-vault-secrets",
    path=ocw_studio_secrets.path.apply("{}/collected".format),
    data_json=json.dumps(vault_secrets),
)


gh_repo = github.get_repository(
    full_name="mitodl/ocw-hugo-projects", opts=InvokeOptions(provider=github_provider)
)
ocw_starter_webhook = github.RepositoryWebhook(
    "ocw-hugo-project-sync-with-ocw-studio-webhook",
    repository=gh_repo.name,
    events=["push"],
    active=True,
    configuration=github.RepositoryWebhookConfigurationArgs(
        url="https://{}/api/starters/site_configs/".format(
            ocw_studio_config.require("app_domain")
        ),
        content_type="json",
        secret=vault_secrets["github"]["shared_secret"],
    ),
    opts=github_options,
)

# Setup AWS MediaConvert Queue
ocw_studio_mediaconvert_config = MediaConvertConfig(
    service_name="ocw-studio",
    env_suffix=stack_info.env_suffix,
    tags=aws_config.tags,
    policy_arn=ocw_studio_iam_policy.arn,
    host=ocw_studio_config.require("app_domain"),
)

ocw_studio_mediaconvert = OLMediaConvert(ocw_studio_mediaconvert_config)

env_name = stack_info.name.lower() if stack_info.name != "QA" else "rc"

# Non-sensitive env vars shared between Heroku and K8s deployments
app_env_vars = {
    "ALLOWED_HOSTS": '["*"]',
    "AWS_ACCOUNT_ID": aws_account.id,
    "AWS_ARTIFACTS_BUCKET_NAME": "ol-eng-artifacts",
    "AWS_MAX_CONCURRENT_CONNECTIONS": 100,
    "AWS_OFFLINE_PREVIEW_BUCKET_NAME": f"ocw-content-offline-draft-{stack_info.env_suffix}",
    "AWS_OFFLINE_PUBLISH_BUCKET_NAME": f"ocw-content-offline-live-{stack_info.env_suffix}",
    "AWS_OFFLINE_TEST_BUCKET_NAME": f"ocw-content-offline-test-{stack_info.env_suffix}",
    "AWS_PREVIEW_BUCKET_NAME": f"ocw-content-draft-{stack_info.env_suffix}",
    "AWS_PUBLISH_BUCKET_NAME": f"ocw-content-live-{stack_info.env_suffix}",
    "AWS_REGION": "us-east-1",
    "AWS_ROLE_NAME": ocw_studio_mediaconvert.role.name,
    "AWS_STORAGE_BUCKET_NAME": f"ol-ocw-studio-app-{stack_info.env_suffix}",
    "AWS_TEST_BUCKET_NAME": f"ocw-content-test-{stack_info.env_suffix}",
    "CONCOURSE_USERNAME": "oldevops",
    "CONTENT_SYNC_BACKEND": "content_sync.backends.github.GithubBackend",
    "CONTENT_SYNC_PIPELINE": "content_sync.pipelines.concourse.ConcourseGithubPipeline",
    "CONTENT_SYNC_THEME_PIPELINE": "content_sync.pipelines.concourse.ThemeAssetsPipeline",
    "ENV_NAME": env_name,
    "GIT_DOMAIN": "github.mit.edu",
    "GIT_API_URL": "https://github.mit.edu/api/v3",
    "GIT_DEFAULT_USER_NAME": "OCW Studio Bot",
    "MITOL_MAIL_FROM_EMAIL": "ocw-prod-support@mit.edu",
    "MITOL_MAIL_REPLY_TO_ADDRESS": "ocw-prod-support@mit.edu",
    "OCW_COURSE_TEST_SLUG": "ocw-ci-test-course",
    "OCW_DEFAULT_COURSE_THEME": "ocw-course-v2",
    "OCW_STUDIO_SECURE_SSL_REDIRECT": False,
    "OCW_STUDIO_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "OCW_STUDIO_DB_CONN_MAX_AGE": 0,
    "OCW_STUDIO_DB_DISABLE_SSL": "True",
    "OCW_STUDIO_DELETABLE_CONTENT_TYPES": "external-resource,instructor,page,course-collection,resource",
    "OCW_STUDIO_ENVIRONMENT": env_name,
    "OCW_STUDIO_USE_S3": "True",
    "OCW_WWW_TEST_SLUG": "ocw-ci-test-www",
    "POSTHOG_ENABLED": "True",
    "POSTHOG_PROJECT_API_KEY": "phc_XDgBzghi6cHYiBbiTsL91Fw03j073dXNSxtG7MWfeS0",  # pragma: allowlist secret
    "PREPUBLISH_ACTIONS": "videos.tasks.update_transcripts_for_website,videos.youtube.update_youtube_metadata,content_sync.tasks.update_website_in_root_website",
    "SOCIAL_AUTH_SAML_CONTACT_NAME": "Open Learning Support",
    "SOCIAL_AUTH_SAML_IDP_ATTRIBUTE_EMAIL": "urn:oid:0.9.2342.19200300.100.1.3",
    "SOCIAL_AUTH_SAML_IDP_ATTRIBUTE_NAME": "urn:oid:2.16.840.1.113730.3.1.241",
    "SOCIAL_AUTH_SAML_IDP_ATTRIBUTE_PERM_ID": "urn:oid:1.3.6.1.4.1.5923.1.1.1.6",
    "SOCIAL_AUTH_SAML_IDP_ENTITY_ID": "https://idp.mit.edu/shibboleth",
    "SOCIAL_AUTH_SAML_IDP_URL": "https://idp.mit.edu/idp/profile/SAML2/Redirect/SSO",
    "SOCIAL_AUTH_SAML_LOGIN_URL": "https://idp.mit.edu/idp/profile/SAML2/Redirect/SSO",
    "SOCIAL_AUTH_SAML_ORG_DISPLAYNAME": "MIT Open Learning",
    "SOCIAL_AUTH_SAML_SECURITY_ENCRYPTED": "True",
    "USE_X_FORWARDED_PORT": "True",
    "VIDEO_S3_TRANSCODE_PREFIX": "aws_mediaconvert_transcodes",
    "VIDEO_TRANSCODE_QUEUE": ocw_studio_mediaconvert.queue.name,
}

if k8s_deploy:
    ################################
    # Kubernetes Deployment Path   #
    ################################
    vault_config = Config("vault")
    redis_config = Config("redis")

    cluster_stack = StackReference(
        f"infrastructure.aws.eks.applications.{stack_info.name}"
    )
    cluster_substructure_stack = StackReference(
        f"substructure.aws.eks.applications.{stack_info.name}"
    )
    vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
    k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

    k8s_app_labels = K8sAppLabels(
        application=Application.ocw_studio,
        product=Product.infrastructure,
        service=Services.ocw_studio,
        ou=BusinessUnit.ocw,
        source_repository="https://github.com/mitodl/ocw-studio",
        stack=stack_info,
    ).model_dump()

    setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))
    ocw_studio_namespace = "ocw-studio"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(ocw_studio_namespace, ns)
    )

    # Pod security group for the OCW Studio application
    ocw_studio_app_security_group = ec2.SecurityGroup(
        f"ocw-studio-app-access-{stack_info.env_suffix}",
        description=f"Access control for the OCW Studio App in {stack_info.name}",
        egress=default_psg_egress_args,
        ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
        tags=aws_config.tags,
        vpc_id=apps_vpc["id"],
    )

    # Redis / Elasticache
    redis_cluster_security_group = ec2.SecurityGroup(
        f"ocw-studio-redis-cluster-security-group-{stack_info.env_suffix}",
        name_prefix=f"ocw-studio-redis-cluster-security-group-{stack_info.env_suffix}",
        description="Access control for the OCW Studio redis cluster.",
        ingress=[
            ec2.SecurityGroupIngressArgs(
                security_groups=[
                    ocw_studio_app_security_group.id,
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
    redis_defaults = defaults(stack_info)["redis"]
    redis_defaults["instance_type"] = (
        redis_config.get("instance_type") or redis_defaults["instance_type"]
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
        cluster_description="Redis cluster for OCW Studio",
        cluster_name=f"ocw-studio-app-redis-{stack_info.env_suffix}",
        subnet_group=apps_vpc["elasticache_subnet"],
        security_groups=[redis_cluster_security_group.id],
        tags=aws_config.tags,
        **redis_defaults,
    )
    redis_cache = OLAmazonCache(redis_cache_config)

    # Vault policy and K8s auth
    ocw_studio_vault_policy_template = (
        Path(__file__).parent.joinpath("ocw_studio_policy.hcl").read_text()
    )
    ocw_studio_vault_policy_text = ocw_studio_vault_policy_template.replace(
        "DEPLOYMENT", stack_info.env_suffix
    )
    ocw_studio_vault_policy = vault.Policy(
        f"ocw-studio-vault-policy-{stack_info.env_suffix}",
        name="ocw-studio",
        policy=ocw_studio_vault_policy_text,
    )

    ocw_studio_vault_k8s_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"ocw-studio-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=Services.ocw_studio,
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[ocw_studio_namespace],
        token_policies=[ocw_studio_vault_policy.name],
    )

    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name=Services.ocw_studio,
            namespace=ocw_studio_namespace,
            labels=k8s_app_labels,
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=ocw_studio_vault_k8s_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(delete_before_replace=True),
    )

    # RDS endpoint for K8s secret templates
    db_instance_name = f"ocw-studio-db-applications-{stack_info.env_suffix}"
    rds_endpoint = f"{db_instance_name}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:{DEFAULT_POSTGRES_PORT}"

    # Create Kubernetes secrets
    secret_names, secret_resources = create_ocw_studio_k8s_secrets(
        stack_info=stack_info,
        ocw_studio_namespace=ocw_studio_namespace,
        k8s_global_labels=k8s_app_labels,
        vault_k8s_resources=vault_k8s_resources,
        db_config=ocw_studio_vault_backend,
        rds_endpoint=rds_endpoint,
        redis_password=redis_config.require("password"),
        redis_cache=redis_cache,
    )

    # Merge stack-level config vars into the app env vars
    app_env_vars.update(**ocw_studio_config.get_object("vars") or {})
    app_env_vars["POSTHOG_API_HOST"] = app_env_vars.pop(
        "PUBLISH_POSTHOG_API_HOST",
        ocw_studio_config.get("posthog_api_host") or "https://app.posthog.com",
    )

    if "OCW_STUDIO_DOCKER_TAG" not in os.environ:
        msg = "OCW_STUDIO_DOCKER_TAG must be set."
        raise OSError(msg)
    OCW_STUDIO_DOCKER_TAG = os.environ["OCW_STUDIO_DOCKER_TAG"]

    ocw_studio_k8s_app = OLApplicationK8s(
        ol_app_k8s_config=OLApplicationK8sConfig(
            project_root=Path(__file__).parent,
            application_config=app_env_vars,
            application_name=Services.ocw_studio,
            application_namespace=ocw_studio_namespace,
            application_lb_service_name="ocw-studio-webapp",
            application_lb_service_port_name="http",
            application_min_replicas=ocw_studio_config.get_int("min_replicas") or 2,
            k8s_global_labels=k8s_app_labels,
            env_from_secret_names=secret_names,
            application_security_group_id=ocw_studio_app_security_group.id,
            application_security_group_name=ocw_studio_app_security_group.name,
            application_image_repository="mitodl/ocw-studio-app",
            application_docker_tag=OCW_STUDIO_DOCKER_TAG,
            application_cmd_array=["uwsgi"],
            application_arg_array=["/tmp/uwsgi.ini"],  # noqa: S108
            vault_k8s_resource_auth_name=vault_k8s_resources.auth_name,
            registry="dockerhub",
            import_nginx_config=True,
            import_nginx_config_path="files/web.conf",
            import_uwsgi_config=True,
            init_migrations=True,
            init_collectstatic=True,
            pre_deploy_commands=[
                ("migrate", ["python", "manage.py", "migrate", "--noinput"])
            ],
            celery_worker_configs=[
                OLApplicationK8sCeleryWorkerConfig(
                    queue_name="default",
                    redis_host=redis_cache.address,
                    redis_password=redis_config.require("password"),
                ),
                OLApplicationK8sCeleryWorkerConfig(
                    queue_name="publish",
                    redis_host=redis_cache.address,
                    redis_password=redis_config.require("password"),
                ),
                OLApplicationK8sCeleryWorkerConfig(
                    queue_name="batch",
                    redis_host=redis_cache.address,
                    redis_password=redis_config.require("password"),
                ),
            ],
            resource_requests={"cpu": "250m", "memory": "1Gi"},
            resource_limits={"memory": "1Gi"},
            # App lacks health check endpoints so we use nginx's
            probe_configs={
                "liveness_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health",
                        port=DEFAULT_NGINX_PORT,
                    ),
                    initial_delay_seconds=30,
                    period_seconds=30,
                    failure_threshold=3,
                    timeout_seconds=3,
                ),
                "readiness_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health",
                        port=DEFAULT_NGINX_PORT,
                    ),
                    initial_delay_seconds=15,
                    period_seconds=15,
                    failure_threshold=3,
                    timeout_seconds=3,
                ),
                "startup_probe": kubernetes.core.v1.ProbeArgs(
                    http_get=kubernetes.core.v1.HTTPGetActionArgs(
                        path="/nginx-health",
                        port=DEFAULT_NGINX_PORT,
                    ),
                    initial_delay_seconds=10,
                    period_seconds=10,
                    failure_threshold=6,
                    success_threshold=1,
                    timeout_seconds=5,
                ),
            },
        ),
        opts=ResourceOptions(
            depends_on=[ocw_studio_app_security_group, *secret_resources]
        ),
    )

    # APISIX routing (pass-through, no OIDC)
    app_domain = ocw_studio_config.require("app_domain")
    tls_secret_name = "ocw-studio-tls-pair"  # noqa: S105  # pragma: allowlist secret

    cert_manager_certificate = OLCertManagerCert(
        f"ocw-studio-cert-manager-certificate-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="ocw-studio",
            k8s_namespace=ocw_studio_namespace,
            k8s_labels=k8s_app_labels,
            create_apisixtls_resource=True,
            dest_secret_name=tls_secret_name,
            dns_names=[app_domain],
        ),
    )

    ocw_studio_apisix_httproute = OLApisixHTTPRoute(
        f"ocw-studio-apisix-httproute-{stack_info.env_suffix}",
        route_configs=[
            OLApisixHTTPRouteConfig(
                route_name="passthrough",
                hosts=[app_domain],
                paths=["/*"],
                backend_service_name=ocw_studio_k8s_app.application_lb_service_name,
                backend_service_port=ocw_studio_k8s_app.application_lb_service_port_name,
                plugins=[],
            ),
        ],
        k8s_namespace=ocw_studio_namespace,
        k8s_labels=k8s_app_labels,
    )

################################
# Heroku Deployment Path       #
################################
heroku_app_vars = heroku_app_config.get_object("vars")
heroku_vars = dict(app_env_vars)
heroku_vars["POSTHOG_API_HOST"] = heroku_app_vars.get(
    "PUBLISH_POSTHOG_API_HOST", "https://app.posthog.com"
)
heroku_vars.update(**heroku_app_vars)

auth_aws_mitx_creds_ocw_studio_app_env = vault.generic.get_secret_output(
    path=f"aws-mitx/creds/ocw-studio-app-{stack_info.env_suffix}",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=ocw_studio_secrets),
)

secret_global_mailgun_api_key = vault.generic.get_secret_output(
    path="secret-global/mailgun",
    opts=InvokeOptions(parent=ocw_studio_secrets),
)

secret_concourse_ocw_api_bearer_token = vault.generic.get_secret_output(
    path="secret-concourse/ocw/api-bearer-token",
    opts=InvokeOptions(parent=ocw_studio_secrets),
)

secret_concourse_web = vault.generic.get_secret_output(
    path="secret-concourse/web", opts=InvokeOptions(parent=ocw_studio_secrets)
)

sensitive_heroku_vars = {
    "AWS_ACCESS_KEY_ID": auth_aws_mitx_creds_ocw_studio_app_env.data.apply(
        lambda data: "{}".format(data["access_key"])
    ),
    "AWS_SECRET_ACCESS_KEY": auth_aws_mitx_creds_ocw_studio_app_env.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "MAILGUN_KEY": secret_global_mailgun_api_key.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
    "API_BEARER_TOKEN": secret_concourse_ocw_api_bearer_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "CONCOURSE_PASSWORD": secret_concourse_web.data.apply(
        lambda data: "{}".format(data["admin_password"])
    ),
    "DRIVE_SERVICE_ACCOUNT_CREDS": vault_secrets["google"]["drive_service_json"],
    "GIT_TOKEN": vault_secrets["github"]["user_token"],
    "OPEN_CATALOG_WEBHOOK_KEY": vault_secrets["open_catalog_webhook_key"],
    "SECRET_KEY": vault_secrets["django"]["secret_key"],
    "SENTRY_DSN": vault_secrets["sentry_dsn"],
    "SOCIAL_AUTH_SAML_IDP_X509": vault_secrets["saml"]["idp_x509"],
    "SOCIAL_AUTH_SAML_SP_PRIVATE_KEY": vault_secrets["saml"]["sp_private_key"],
    "SOCIAL_AUTH_SAML_SP_PUBLIC_CERT": vault_secrets["saml"]["sp_public_cert"],
    "STATUS_TOKEN": vault_secrets["django"]["status_token"],
    "THREEPLAY_API_KEY": vault_secrets["threeplay"]["api_key"],
    "THREEPLAY_CALLBACK_KEY": vault_secrets["threeplay"]["callback_key"],
    "YT_ACCESS_TOKEN": vault_secrets["youtube"]["access_token"],
    "YT_CLIENT_ID": vault_secrets["youtube"]["client_id"],
    "YT_CLIENT_SECRET": vault_secrets["youtube"]["client_secret"],
    "YT_REFRESH_TOKEN": vault_secrets["youtube"]["refresh_token"],
    "VIDEO_S3_TRANSCODE_ENDPOINT": vault_secrets["transcode_endpoint"],
    "GITHUB_APP_PRIVATE_KEY": vault_secrets["github"]["app_private_key"],
    "GITHUB_WEBHOOK_KEY": vault_secrets["github"]["shared_secret"],
}

auth_postgres_ocw_studio_applications_env_creds_app = vault.generic.get_secret_output(
    path=f"postgres-ocw-studio-applications-{stack_info.env_suffix}/creds/app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=ocw_studio_secrets),
)
sensitive_heroku_vars["DATABASE_URL"] = (
    auth_postgres_ocw_studio_applications_env_creds_app.data.apply(
        lambda data: (
            "postgres://{}:{}@ocw-studio-db-applications-{}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/ocw_studio".format(
                data["username"], data["password"], stack_info.env_suffix
            )
        )
    )
)

heroku_app_id = heroku_config.require("app_id")
ocw_studio_heroku_configassociation = heroku.app.ConfigAssociation(
    f"ocw-studio-heroku-configassociation-{stack_info.env_suffix}",
    app_id=heroku_app_id,
    sensitive_vars=sensitive_heroku_vars,
    vars=heroku_vars,
)

export(
    "ocw_studio_app",
    {
        "rds_host": ocw_studio_db.db_instance.address,
        "mediaconvert_queue": ocw_studio_mediaconvert.queue.id,
    },
)
