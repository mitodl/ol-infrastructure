"""Create the infrastructure and services needed to support the OCW Studio application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json
from pathlib import Path

import pulumi_github as github
import pulumi_vault as vault
import pulumiverse_heroku as heroku
from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference, export
from pulumi_aws import cloudwatch, ec2, iam, mediaconvert, s3, sns

from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider(skip_child_token=True)
setup_heroku_provider()
github_provider = github.Provider(
    "github-provider",
    owner=read_yaml_secrets(Path(f"pulumi/github_provider.yaml"))["owner"],  # noqa: F541
    token=read_yaml_secrets(Path(f"pulumi/github_provider.yaml"))["token"],  # noqa: F541
)
github_options = ResourceOptions(provider=github_provider)
ocw_studio_config = Config("ocw_studio")
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")
stack_info = parse_stack()
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

# Create S3 buckets

# Bucket used to store output from ocw-to-hugo which is markdown files rendered from
# legacy OCW Plone content.
ocw_to_hugo_bucket_name = f"ocw-to-hugo-output-{stack_info.env_suffix}"
ocw_studio_legacy_markdown_bucket = s3.Bucket(
    f"ocw-to-hugo-output-{stack_info.env_suffix}",
    bucket=ocw_to_hugo_bucket_name,
    tags=aws_config.tags,
)

# Bucket used to store file uploads from ocw-studio app.
ocw_storage_bucket_name = f"ol-ocw-studio-app-{stack_info.env_suffix}"
ocw_storage_bucket = s3.BucketV2(
    f"ol-ocw-studio-app-{stack_info.env_suffix}",
    bucket=ocw_storage_bucket_name,
    tags=aws_config.tags,
)
ocw_storage_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-ocw-studio-app-ownership-controls",
    bucket=ocw_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioningV2(
    "ol-ocw-studio-app-bucket-versioning",
    bucket=ocw_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningV2VersioningConfigurationArgs(
        status="Enabled"
    ),
)
ocw_storage_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-ocw-studio-app-bucket-public-access-controls",
    bucket=ocw_storage_bucket.id,
    block_public_policy=False,
)
s3.BucketPolicy(
    "ol-ocw-studio-app-bucket-policy",
    bucket=ocw_storage_bucket.id,
    policy=json.dumps(
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
    opts=ResourceOptions(
        depends_on=[
            ocw_storage_bucket_public_access,
            ocw_storage_bucket_ownership_controls,
        ]
    ),
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
                        "s3:ListBucket*",
                        "s3:GetObject*",
                    ],
                    "Resource": [
                        f"arn:aws:s3:::{ocw_to_hugo_bucket_name}",
                        f"arn:aws:s3:::{ocw_to_hugo_bucket_name}/*",
                    ],
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
                {
                    "Effect": "Allow",
                    "Action": [
                        "mediaconvert:ListQueues",
                        "mediaconvert:DescribeEndpoints",
                        "mediaconvert:ListPresets",
                        "mediaconvert:CreatePreset",
                        "mediaconvert:DisassociateCertificate",
                        "mediaconvert:CreateQueue",
                        "mediaconvert:AssociateCertificate",
                        "mediaconvert:CreateJob",
                        "mediaconvert:ListJobTemplates",
                    ],
                    "Resource": "*",
                },
                {
                    "Effect": "Allow",
                    "Action": "mediaconvert:GetJob",
                    "Resource": "arn:*:mediaconvert:*:*:jobs/*",
                },
                {
                    "Effect": "Allow",
                    "Action": "iam:PassRole",
                    "Resource": f"arn:aws:iam::610119931565:role/service-role-mediaconvert-ocw-studio-{stack_info.env_suffix}",  # noqa: E501
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

ocw_studio_mediaconvert_role = iam.Role(
    "ocw-studio-mediaconvert-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "mediaconvert.amazonaws.com"},
            },
        }
    ),
    name=f"service-role-mediaconvert-ocw-studio-{stack_info.env_suffix}",
    tags=aws_config.tags,
)

iam.RolePolicyAttachment(
    f"ocw-studio-{stack_info.env_suffix}-mediaconvert-role-policy",
    policy_arn=ocw_studio_iam_policy.arn,
    role=ocw_studio_mediaconvert_role.name,
)

ocw_studio_vault_backend_role = vault.aws.SecretBackendRole(
    f"ocw-studio-app-{stack_info.env_suffix}",
    name=f"ocw-studio-app-{stack_info.env_suffix}",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[ocw_studio_iam_policy.arn],
)

# Create RDS instance
ocw_studio_db_security_group = ec2.SecurityGroup(
    f"ocw-studio-db-access-{stack_info.env_suffix}",
    description=f"Access control for the OCW Studio DB in {stack_info.name}",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            description="Allow access over the public internet from Heroku",
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            security_groups=[data_vpc["security_groups"]["integrator"]],
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
    tags=aws_config.merged_tags(
        {"name": "ocw-studio-db-access-applications-{stack_info.env_suffix}"}
    ),
    vpc_id=apps_vpc["id"],
)

ocw_studio_db_config = OLPostgresDBConfig(
    instance_name=f"ocw-studio-db-applications-{stack_info.env_suffix}",
    password=ocw_studio_config.require("db_password"),
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[ocw_studio_db_security_group],
    tags=aws_config.tags,
    db_name="ocw_studio",
    public_access=True,
    **defaults(stack_info)["rds"],
)
ocw_studio_db = OLAmazonDB(ocw_studio_db_config)

db_creds_mount_point = (
    f"{ocw_studio_db_config.engine}-ocw-studio-applications-{stack_info.env_suffix}"
)
ocw_studio_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=ocw_studio_db_config.db_name,
    mount_point=db_creds_mount_point,
    db_admin_username=ocw_studio_db_config.username,
    db_admin_password=ocw_studio_db_config.password.get_secret_value(),
    db_host=ocw_studio_db.db_instance.address,
    **defaults(stack_info)["rds"],
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
)
vault.generic.Secret(
    "ocw-studio-vault-secrets",
    path=ocw_studio_secrets.path.apply("{}/app-config".format),
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
        secret=vault_secrets["github_shared_secret"],
    ),
    opts=github_options,
)

# Setup AWS MediaConvert Queue
ocw_studio_mediaconvert_queue = mediaconvert.Queue(
    "ocw-studio-mediaconvert-queue",
    description="OCW Studio Queue",
    name=f"ocw-studio-mediaconvert-queue-{stack_info.env_suffix}",
    tags=aws_config.tags,
)

# Configure SNS Topic and Subscription
ocw_studio_sns_topic = sns.Topic(
    f"ocw-studio-{stack_info.env_suffix}-sns-topic", tags=aws_config.tags
)

ocw_studio_sns_topic_subscription = sns.TopicSubscription(
    "ocw-studio-sns-topic-subscription",
    endpoint="https://{}/api/transcode-jobs/".format(
        ocw_studio_config.require("app_domain")
    ),
    protocol="https",
    raw_message_delivery=True,
    topic=ocw_studio_sns_topic.arn,
)

# Configure Cloudwatch EventRule and EventTarget
ocw_studio_mediaconvert_cloudwatch_rule = cloudwatch.EventRule(
    "ocw-studio-mediaconvert-cloudwatch-eventrule",
    description="Capture MediaConvert Events for use with OCW Studio",
    event_pattern=json.dumps(
        {
            "source": ["aws.mediaconvert"],
            "detail-type": ["MediaConvert Job State Change"],
            "detail": {
                "userMetadata": {
                    "filter": [f"ocw-studio-mediaconvert-queue-{stack_info.env_suffix}"]
                },
                "status": ["COMPLETE", "ERROR"],
            },
        }
    ),
)

ocw_studio_mediaconvert_cloudwatch_target = cloudwatch.EventTarget(
    "ocw-studio-mediaconvert-cloudwatch-eventtarget",
    rule=ocw_studio_mediaconvert_cloudwatch_rule.name,
    arn=ocw_studio_sns_topic.arn,
)

# Heroku App configuration

# Handle various naming nuances present in ocw-studio
# env_name is 'ci' 'rc' or 'production'
env_name = stack_info.name.lower() if stack_info.name != "QA" else "rc"

# env is 'qa' 'qa' or 'production
env = "qa" if stack_info.name != "Production" else "production"

# vault_env_path is 'rc-apps' rc-apps' or 'production-apps'
vault_env_path = "rc-apps" if stack_info.name != "Production" else "production-apps"

heroku_vars = {
    "ALLOWED_HOSTS": '["*"]',
    "AWS_REGION": "us-east-1",
    "AWS_ARTIFACTS_BUCKET_NAME": "ol-eng-artifacts",
    "AWS_MAX_CONCURRENT_CONNECTIONS": 100,
    "CONTENT_SYNC_BACKEND": "content_sync.backends.github.GithubBackend",
    "CONTENT_SYNC_PIPELINE": "content_sync.pipelines.concourse.ConcourseGithubPipeline",
    "CONTENT_SYNC_THEME_PIPELINE": "content_sync.pipelines.concourse.ThemeAssetsPipeline",  # noqa: E501
    "CONCOURSE_USERNAME": "oldevops",
    "DRIVE_S3_UPLOAD_PREFIX": "gdrive_uploads",
    "GIT_API_URL": "https://github.mit.edu/api/v3",
    "GIT_DEFAULT_USER_NAME": "OCW Studio Bot",
    "MITOL_MAIL_FROM_EMAIL": "ocw-prod-support@mit.edu",
    "MITOL_MAIL_REPLY_TO_ADDRESS": "ocw-prod-support@mit.edu",
    "OCW_STUDIO_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "OCW_STUDIO_DB_CONN_MAX_AGE": 0,
    "OCW_STUDIO_DB_DISABLE_SSL": "True",
    "OCW_STUDIO_USE_S3": "True",
    "OCW_WWW_TEST_SLUG": "ocw-ci-test-www",
    "OCW_COURSE_TEST_SLUG": "ocw-ci-test-course",
    "PREPUBLISH_ACTIONS": "videos.tasks.update_transcripts_for_website,videos.youtube.update_youtube_metadata,content_sync.tasks.update_website_in_root_website",  # noqa: E501
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
}

heroku_interpolated_vars = {
    "AWS_OFFLINE_PREVIEW_BUCKET_NAME": f"ocw-content-offline-draft-{env_name}",
    "AWS_OFFLINE_PUBLISH_BUCKET_NAME": f"ocw-content-offline-live-{env_name}",
    "AWS_OFFLINE_TEST_BUCKET_NAME": f"ocw-content-offline-test-{env_name}",
    "AWS_PREVIEW_BUCKET_NAME": f"ocw-content-draft-{env_name}",
    "AWS_PUBLISH_BUCKET_NAME": f"ocw-content-live-{env_name}",
    "AWS_ROLE_NAME": f"service-role-mediaconvert-ocw-studio-{env_name}",
    "AWS_STORAGE_BUCKET_NAME": f"ol-ocw-studio-app-{env_name}",
    "AWS_TEST_BUCKET_NAME": f"ocw-content-test-{env_name}",
    "ENV_NAME": env_name,
    "OCW_STUDIO_ENVIRONMENT": env_name,
}

# Combine the two var sources above wit hvalues explicitly defined in
# pulumi configuration
heroku_vars.update(**heroku_interpolated_vars)
heroku_vars.update(**heroku_app_config.get_object("vars"))

# Setup the various sensitive a secret vars
auth_aws_mitx_creds_ocw_studio_app_env = vault.generic.get_secret_output(
    path=f"aws-mitx/creds/ocw-studio-app-{env}",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
)

# secret-concourse/*
secret_concourse_ocw_api_bearer_token = vault.generic.get_secret_output(
    path="secret-concourse/ocw/api-bearer-token",
    opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
)
secret_concourse_web = vault.generic.get_secret_output(
    path="secret-concourse/web",
    opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
)

# secret-operations/*
secret_operations_global_mailgun_api_key = vault.generic.get_secret_output(
    path="secret-operations/global/mailgun-api-key",
    opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
)
secret_operations_global_open_courseware_ocw_studio_sentry_dsn = (
    vault.generic.get_secret_output(
        path="secret-operations/global/open-courseware-ocw-studio-sentry-dsn",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
)
secret_operations_vault_env_path_open_courseware_saml = vault.generic.get_secret_output(
    path=f"secret-operations/{vault_env_path}/open-courseware/saml",
    opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
)
secret_operations_global_open_courseware_ocw_studio_threeplay_api_key = (
    vault.generic.get_secret_output(
        path="secret-operations/global/open-courseware/ocw-studio/threeplay_api_key",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
)
secret_operations_global_open_courseware_ocw_studio_threeplay_callback_key = vault.generic.get_secret_output(  # noqa: E501
    path="secret-operations/global/open-courseware/ocw-studio/threeplay_callback_key",
    opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
)

# secret-open-courseware/*
secret_open_courseware_ocw_studio_env_aws_account_id = vault.generic.get_secret_output(
    path=f"secret-open-courseware/ocw_studio/{env}/aws_account_id",
    opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
)
secret_open_courseware_ocw_studio_env_gdrive_service_json = (
    vault.generic.get_secret_output(
        path=f"secret-open-courseware/ocw_studio/{env}/gdrive-service-json",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
)
secret_open_courseware_ocw_studio_env_github_user_token = (
    vault.generic.get_secret_output(
        path=f"secret-open-courseware/ocw_studio/{env}/github-user-token",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
)
secret_open_courseware_ocw_studio_env_django_secret_key = (
    vault.generic.get_secret_output(
        path=f"secret-open-courseware/ocw-studio/{env}/django-secret-key",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
)
secret_open_courseware_ocw_studio_env_django_status_token = (
    vault.generic.get_secret_output(
        path=f"secret-open-courseware/ocw-studio/{env}/django-status-token",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
)
secret_open_courseware_global_update_search_data_webhook_key = (
    vault.generic.get_secret_output(
        path="secret-open-courseware/global/update-search-data-webhook-key",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
)
secret_open_courseware_ocw_studio_vault_env_path_youtube_credentials = (
    vault.generic.get_secret_output(
        path=f"secret-open-courseware/ocw-studio/{vault_env_path}/youtube-credentials",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
)

sensitive_heroku_vars = {
    # Locally available secrets
    "GITHUB_APP_PRIVATE_KEY": vault_secrets["github_app_private_key"],
    "GITHUB_WEBHOOK_KEY": vault_secrets["github_shared_secret"],
    # Secrets from mounts created/populated outside of this file
    # secret-concourse
    "CONCOURSE_PASSWORD": secret_concourse_web.data.apply(
        lambda data: "{}".format(data["admin_password"])
    ),
    # secret-operations
    "MAILGUN_KEY": secret_operations_global_mailgun_api_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
}

# None of the secrets below are present in CI
# The CI environment gets it database from heroku.
# Other envs need to look up db creds from vault
# Additionally, this value in secret-ocw-studio seems
# to have been manually created and only in QA+Production
if stack_info.env_suffix != "ci":
    auth_postgres_ocw_studio_creds_app = vault.generic.get_secret_output(
        path=f"{db_creds_mount_point}/creds/app",
        with_lease_start_time=False,
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
    sensitive_heroku_vars[
        "DATABASE_URL"
    ] = auth_postgres_ocw_studio_creds_app.data.apply(
        lambda data: "postgres://{}:{}@{}/ocw_studio".format(
            data["username"], data["password"], ocw_studio_db.db_instance.address
        )
    )

    secret_ocw_studio_video_s3_transcode_endpoint = vault.generic.get_secret_output(
        path="secret-ocw-studio/video_s3_transcode_endpoint",
        opts=InvokeOptions(parent=ocw_studio_vault_backend_role),
    )
    # TODO @Ardiea: 20240206 Figure out the deal with the secret below  # noqa: TD003, FIX002, E501
    sensitive_heroku_vars[
        "VIDEO_S3_TRANSCODE_ENDPOINT"
    ] = secret_ocw_studio_video_s3_transcode_endpoint.data.apply(
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars["API_BEARER_TOKEN"] = (
        secret_concourse_ocw_api_bearer_token.data.apply(
            lambda data: "{}".format(data["value"])
        ),
    )

    # More complicated auth configurations
    sensitive_heroku_vars["AWS_ACCESS_KEY_ID"] = (
        auth_aws_mitx_creds_ocw_studio_app_env.data.apply(
            lambda data: "{}".format(data["access_key"])
        ),
    )
    sensitive_heroku_vars["AWS_SECRET_ACCESS_KEY"] = (
        auth_aws_mitx_creds_ocw_studio_app_env.data.apply(
            lambda data: "{}".format(data["secret_key"])
        ),
    )

    # secret-open-courseware
    sensitive_heroku_vars[
        "AWS_ACCOUNT_ID"
    ] = secret_open_courseware_ocw_studio_env_aws_account_id.data.apply(
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars[
        "DRIVE_SERVICE_ACCOUNT_CREDS"
    ] = secret_open_courseware_ocw_studio_env_gdrive_service_json.data.apply(
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars[
        "GIT_TOKEN"
    ] = secret_open_courseware_ocw_studio_env_github_user_token.data.apply(
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars[
        "OPEN_CATALOG_WEBHOOK_KEY"
    ] = secret_open_courseware_global_update_search_data_webhook_key.data.apply(
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars[
        "SECRET_KEY"
    ] = secret_open_courseware_ocw_studio_env_django_secret_key.data.apply(
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars[
        "STATUS_TOKEN"
    ] = secret_open_courseware_ocw_studio_env_django_status_token.data.apply(
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars[
        "YT_ACCESS_TOKEN"
    ] = secret_open_courseware_ocw_studio_vault_env_path_youtube_credentials.data.apply(
        lambda data: "{}".format(data["access_token"])
    )
    sensitive_heroku_vars[
        "YT_CLIENT_ID"
    ] = secret_open_courseware_ocw_studio_vault_env_path_youtube_credentials.data.apply(
        lambda data: "{}".format(data["client_id"])
    )
    sensitive_heroku_vars[
        "YT_CLIENT_SECRET"
    ] = secret_open_courseware_ocw_studio_vault_env_path_youtube_credentials.data.apply(
        lambda data: "{}".format(data["client_secret"])
    )
    sensitive_heroku_vars[
        "YT_REFRESH_TOKEN"
    ] = secret_open_courseware_ocw_studio_vault_env_path_youtube_credentials.data.apply(
        lambda data: "{}".format(data["refresh_token"])
    )

    sensitive_heroku_vars[
        "THREEPLAY_API_KEY"
    ] = secret_operations_global_open_courseware_ocw_studio_threeplay_api_key.data.apply(  # noqa: E501
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars[
        "THREEPLAY_CALLBACK_KEY"
    ] = secret_operations_global_open_courseware_ocw_studio_threeplay_callback_key.data.apply(  # noqa: E501
        lambda data: "{}".format(data["value"])
    )
    sensitive_heroku_vars[
        "SOCIAL_AUTH_SAML_IDP_X509"
    ] = secret_operations_vault_env_path_open_courseware_saml.data.apply(
        lambda data: "{}".format(data["idp_x509"])
    )
    sensitive_heroku_vars[
        "SOCIAL_AUTH_SAML_SP_PRIVATE_KEY"
    ] = secret_operations_vault_env_path_open_courseware_saml.data.apply(
        lambda data: "{}".format(data["private_key"])
    )
    sensitive_heroku_vars[
        "SOCIAL_AUTH_SAML_SP_PUBLIC_CERT"
    ] = secret_operations_vault_env_path_open_courseware_saml.data.apply(
        lambda data: "{}".format(data["public_cert"])
    )
    sensitive_heroku_vars[
        "SENTRY_DSN"
    ] = secret_operations_global_open_courseware_ocw_studio_sentry_dsn.data.apply(
        lambda data: "{}".format(data["value"])
    )

# Finally, actually create the configassociation resource
heroku_app_id = heroku_config.require("app_id")
ocw_studio_heroku_configassociation = heroku.app.ConfigAssociation(
    f"ocw-studio-{stack_info.env_suffix}-heroku-configassociation",
    app_id=heroku_app_id,
    sensitive_vars=sensitive_heroku_vars,
    vars=heroku_vars,
)

export(
    "ocw_studio_app",
    {
        "rds_host": ocw_studio_db.db_instance.address,
        "mediaconvert_queue": ocw_studio_mediaconvert_queue.id,
    },
)
