"""Create the infrastructure and services needed to support the OCW Studio application.

- Create a Redis instance in AWS Elasticache
- Create a PostgreSQL database in AWS RDS for production environments
- Create an IAM policy to grant access to S3 and other resources
"""

import json
from pathlib import Path

import pulumi_github as github
import pulumi_vault as vault
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
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
github_provider = github.Provider(
    "github-provider",
    owner=read_yaml_secrets(Path(f"pulumi/github_provider.yaml"))["owner"],  # noqa: F541
    token=read_yaml_secrets(Path(f"pulumi/github_provider.yaml"))["token"],  # noqa: F541
)
github_options = ResourceOptions(provider=github_provider)
ocw_studio_config = Config("ocw_studio")
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

ocw_studio_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=ocw_studio_db_config.db_name,
    mount_point=(
        f"{ocw_studio_db_config.engine}-ocw-studio-applications-{stack_info.env_suffix}"
    ),
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

export(
    "ocw_studio_app",
    {
        "rds_host": ocw_studio_db.db_instance.address,
        "mediaconvert_queue": ocw_studio_mediaconvert_queue.id,
    },
)
