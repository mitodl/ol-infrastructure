# TODO: Manage database object creation
# TODO: Add encryption of EBS volumes
import base64
import json
import textwrap
from functools import partial
from pathlib import Path
from string import Template

import pulumi_vault as vault
import yaml
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import (
    acm,
    autoscaling,
    ec2,
    get_caller_identity,
    iam,
    lb,
    route53,
    s3,
    ses,
)
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_MYSQL_PORT,
    DEFAULT_REDIS_PORT,
)
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLMariaDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultMongoDatabaseConfig,
    OLVaultMysqlDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    default_egress_args,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.aws.route53_helper import acm_certificate_validation_records
from ol_infrastructure.lib.ol_types import Apps, AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import mongodb_role_statements, mysql_role_statements

edxapp_config = Config("edxapp")

SSH_ACCESS_KEY_NAME = edxapp_config.get("ssh_key_name") or "oldevops"
MIN_WEB_NODES_DEFAULT = 3
MAX_WEB_NODES_DEFAULT = 15
MIN_WORKER_NODES_DEFAULT = 1
MAX_WORKER_NODES_DEFAULT = 5
FIVE_MINUTES = 60 * 5

stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)
kms_stack = StackReference(f"infrastructure.aws.kms.{stack_info.name}")
kms_s3_key = kms_stack.require_output("kms_s3_data_analytics_key")
edxapp_vpc = network_stack.require_output(f"{stack_info.env_prefix}_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={
        "OU": edxapp_config.require("business_unit"),
        "Environment": env_name,
        "Application": Apps.edxapp,
        "Owner": "platform-engineering",
    }
)

aws_account = get_caller_identity()
consul_security_groups = consul_stack.require_output("security_groups")
edxapp_vpc_id = edxapp_vpc["id"]
edxapp_zone_id = dns_stack.require_output(
    "{}_zone_id".format(edxapp_config.require("dns_zone"))
)
edxapp_domains = edxapp_config.require_object("domains")
edxapp_web_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["edxapp-web-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

edxapp_worker_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["edxapp-worker-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

##############
# S3 Buckets #
##############

mfe_bucket_name = f"{env_name}-edxapp-mfe"
edxapp_mfe_bucket = s3.Bucket(
    "edxapp-mfe-s3-bucket",
    bucket=mfe_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=False),
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{mfe_bucket_name}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)


storage_bucket_name = f"{env_name}-edxapp-storage"
edxapp_storage_bucket = s3.Bucket(
    "edxapp-storage-s3-bucket",
    bucket=storage_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    tags=aws_config.tags,
)

course_bucket_name = f"{env_name}-edxapp-courses"
edxapp_storage_bucket = s3.Bucket(
    "edxapp-courses-s3-bucket",
    bucket=course_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=False),
    tags=aws_config.tags,
)

grades_bucket_name = f"{env_name}-edxapp-grades"
edxapp_storage_bucket = s3.Bucket(
    "edxapp-grades-s3-bucket",
    bucket=grades_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    tags=aws_config.tags,
)

tracking_bucket_name = f"{env_name}-edxapp-tracking"
edxapp_tracking_bucket = s3.Bucket(
    "edxapp-tracking-logs-s3-bucket",
    bucket=tracking_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    acl="private",
    server_side_encryption_configuration=s3.BucketServerSideEncryptionConfigurationArgs(  # noqa: E501
        rule=s3.BucketServerSideEncryptionConfigurationRuleArgs(
            apply_server_side_encryption_by_default=s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs(  # noqa: E501
                sse_algorithm="aws:kms",
                kms_master_key_id=kms_s3_key["id"],
            ),
            bucket_key_enabled=True,
        )
    ),
    tags=aws_config.tags,
)
s3.BucketPublicAccessBlock(
    "edxapp-tracking-bucket-prevent-public-access",
    bucket=edxapp_tracking_bucket.bucket,
    block_public_acls=True,
    block_public_policy=True,
)

########################
# IAM Roles & Policies #
########################

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    }
}

edxapp_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:ListAllMyBuckets",
            "Resource": "*",
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:PutObject",
                "s3:PutObjectAcl",
                "s3:DeleteObject",
                "s3:ListBucket*",
            ],
            "Resource": [
                f"arn:aws:s3:::{storage_bucket_name}",
                f"arn:aws:s3:::{storage_bucket_name}/*",
                f"arn:aws:s3:::{grades_bucket_name}",
                f"arn:aws:s3:::{grades_bucket_name}/*",
                f"arn:aws:s3:::{course_bucket_name}",
                f"arn:aws:s3:::{course_bucket_name}/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": [
                "s3:GetObject*",
                "s3:PutObject",
                "s3:ListBucket",
            ],
            "Resource": [
                f"arn:aws:s3:::{tracking_bucket_name}",
                f"arn:aws:s3:::{tracking_bucket_name}/*",
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["ses:SendEmail", "ses:SendRawEmail"],
            "Resource": [
                "arn:*:ses:*:*:identity/*.mitxonline.mit.edu",
                f"arn:aws:ses:*:*:configuration-set/edxapp-mitxonline-{stack_info.env_suffix}",  # noqa: E501
            ],
        },
        {
            "Effect": "Allow",
            "Action": ["ses:GetSendQuota"],
            "Resource": "*",
        },
    ],
}

edxapp_policy = iam.Policy(
    "edxapp-policy",
    name_prefix="edxapp-policy-",
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        edxapp_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description="AWS access permissions for edX application instances",
)
edxapp_iam_role = iam.Role(
    "edxapp-instance-role",
    assume_role_policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    name_prefix=f"{stack_info.env_prefix}-edxapp-role-{stack_info.env_suffix}-",
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    tags=aws_config.tags,
)
iam.RolePolicyAttachment(
    "edxapp-describe-instances-permission",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=edxapp_iam_role.name,
)
iam.RolePolicyAttachment(
    "edxapp-role-policy",
    policy_arn=edxapp_policy.arn,
    role=edxapp_iam_role.name,
)
edxapp_instance_profile = iam.InstanceProfile(
    f"edxapp-instance-profile-{stack_info.env_suffix}",
    name_prefix=f"{stack_info.env_prefix}-edxapp-role-{stack_info.env_suffix}-",
    role=edxapp_iam_role.name,
    path=f"/ol-applications/edxapp/{stack_info.env_prefix}/",
)
##################################
#     Network Access Control     #
##################################
group_name = f"edxapp-{env_name}"
edxapp_security_group = ec2.SecurityGroup(
    "edxapp-security-group",
    name_prefix=f"{group_name}-",
    ingress=[],
    egress=default_egress_args,
    tags=aws_config.merged_tags({"Name": group_name}),
    vpc_id=edxapp_vpc_id,
)

# Create security group for edxapp MariaDB database
edxapp_db_security_group = ec2.SecurityGroup(
    f"edxapp-db-access-{stack_info.env_suffix}",
    name_prefix=f"edxapp-db-access-{env_name}-",
    description="Access from Edxapp instances to the associated MariaDB database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[edxapp_security_group.id],
            # TODO: Create Vault security group to act as source of allowed
            # traffic. (TMM 2021-05-04)
            cidr_blocks=[
                edxapp_vpc["cidr"],
                operations_vpc["cidr"],
            ],
            protocol="tcp",
            from_port=DEFAULT_MYSQL_PORT,
            to_port=DEFAULT_MYSQL_PORT,
            description="Access to MariaDB from Edxapp web nodes",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=edxapp_vpc_id,
)


##########################
#     Database Setup     #
##########################
edxapp_db_config = OLMariaDBConfig(
    instance_name=f"edxapp-db-{env_name}",
    password=edxapp_config.require("db_password"),
    subnet_group_name=edxapp_vpc["rds_subnet"],
    security_groups=[edxapp_db_security_group],
    tags=aws_config.tags,
    db_name="edxapp",
    engine_version="10.5.8",
    **defaults(stack_info)["rds"],
)
edxapp_db = OLAmazonDB(edxapp_db_config)

edxapp_mysql_role_statements = mysql_role_statements.copy()
edxapp_mysql_role_statements.pop("app")
edxapp_mysql_role_statements["edxapp"] = {
    "create": Template(
        "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES, "
        "CREATE TEMPORARY TABLES, LOCK TABLES ON edxapp.* TO '{{name}}'@'%';"
    ),
    "revoke": Template("DROP USER '{{name}}';"),
}
edxapp_mysql_role_statements["edxapp-csmh"] = {
    "create": Template(
        "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES, "
        "CREATE TEMPORARY TABLES, LOCK TABLES ON edxapp_csmh.* TO '{{name}}'@'%';"
    ),
    "revoke": Template("DROP USER '{{name}}';"),
}
edxapp_mysql_role_statements["xqueue"] = {
    "create": Template(
        "CREATE USER '{{name}}'@'%' IDENTIFIED BY '{{password}}';"
        "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES, "
        "CREATE TEMPORARY TABLES, LOCK TABLES ON xqueue.* TO '{{name}}'@'%';"
    ),
    "revoke": Template("DROP USER '{{name}}';"),
}

edxapp_db_vault_backend_config = OLVaultMysqlDatabaseConfig(
    db_name=edxapp_db_config.db_name,
    mount_point=f"{edxapp_db_config.engine}-{stack_info.env_prefix}",
    db_admin_username=edxapp_db_config.username,
    db_admin_password=edxapp_config.require("db_password"),
    db_host=edxapp_db.db_instance.address,
    role_statements=edxapp_mysql_role_statements,
)
edxapp_db_vault_backend = OLVaultDatabaseBackend(edxapp_db_vault_backend_config)

edxapp_db_consul_node = Node(
    "edxapp-instance-db-node",
    name="edxapp-mysql",
    address=edxapp_db.db_instance.address,
    datacenter=env_name,
)

edxapp_db_consul_service = Service(
    "edxapp-instance-db-service",
    node=edxapp_db_consul_node.name,
    name="edxapp-db",
    port=edxapp_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="edxapp-db",
            interval="10s",
            name="edxapp-db",
            timeout="60s",
            status="passing",
            tcp=f"{edxapp_db.db_instance.address}:{edxapp_db_config.port}",  # noqa: WPS237,E501
        )
    ],
)

#######################
# MongoDB Vault Setup #
#######################
edxapp_mongo_role_statements = mongodb_role_statements
edxapp_mongo_role_statements["edxapp"] = {
    "create": Template(json.dumps({"roles": [{"role": "readWrite"}], "db": "edxapp"})),
    "revoke": Template(json.dumps({"db": "edxapp"})),
}
edxapp_mongo_role_statements["forum"] = {
    "create": Template(json.dumps({"roles": [{"role": "readWrite"}], "db": "forum"})),
    "revoke": Template(json.dumps({"db": "forum"})),
}

edxapp_mongo_vault_config = OLVaultMongoDatabaseConfig(
    db_name="edxapp",
    mount_point=f"mongodb-{stack_info.env_prefix}",
    db_admin_username="admin",
    db_admin_password=edxapp_config.require("mongo_admin_password"),
    db_host=f"mongodb-master.service.{env_name}.consul",
    role_statements=edxapp_mongo_role_statements,
)
edxapp_mongo_vault_backend = OLVaultDatabaseBackend(edxapp_mongo_vault_config)

###########################
# Redis Elasticache Setup #
###########################

redis_config = Config("redis")
redis_cluster_security_group = ec2.SecurityGroup(
    f"edxapp-redis-cluster-{env_name}",
    name_prefix=f"edxapp-redis-{env_name}-",
    description="Grant access to Redis from Open edX",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            protocol="tcp",
            security_groups=[edxapp_security_group.id],
            description="Allow access from edX to Redis for caching and queueing",
        )
    ],
    tags=aws_config.merged_tags({"Name": f"edxapp-redis-{env_name}"}),
    vpc_id=edxapp_vpc_id,
)

redis_cache_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_config.require("auth_token"),
    cluster_mode_enabled=False,
    encrypted=True,
    engine_version="6.x",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_description="Redis cluster for edX platform tasks and caching",
    cluster_name=f"edxapp-redis-{env_name}",
    security_groups=[redis_cluster_security_group.id],
    subnet_group=edxapp_vpc[
        "elasticache_subnet"
    ],  # the name of the subnet group created in the OLVPC component resource
    tags=aws_config.tags,
    **defaults(stack_info)["redis"],
)
edxapp_redis_cache = OLAmazonCache(redis_cache_config)
edxapp_redis_consul_node = Node(
    "edxapp-redis-cache-node",
    name="edxapp-redis",
    address=edxapp_redis_cache.address,
    datacenter=env_name,
)

edxapp_redis_consul_service = Service(
    "edxapp-redis-consul-service",
    node=edxapp_redis_consul_node.name,
    name="edxapp-redis",
    port=redis_cache_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="edxapp-redis",
            interval="10s",
            name="edxapp-redis",
            timeout="60s",
            status="passing",
            tcp=f"{edxapp_redis_cache.address}:{edxapp_redis_cache.cache_cluster.port}",  # noqa: WPS237,E501
        )
    ],
)

########################################
# Create SES Service For edxapp Emails #
########################################

edxapp_mail_domain = edxapp_config.require("mail_domain")
edxapp_ses_domain_identity = ses.DomainIdentity(
    "edxapp-ses-domain-identity",
    domain=edxapp_mail_domain,
)
edxapp_ses_verification_record = route53.Record(
    "edxapp-ses-domain-identity-verification-dns-record",
    zone_id=edxapp_zone_id,
    name=edxapp_ses_domain_identity.id.apply("_amazonses.{}".format),
    type="TXT",
    ttl=FIVE_MINUTES,
    records=[edxapp_ses_domain_identity.verification_token],
)
edxapp_ses_domain_identity_verification = ses.DomainIdentityVerification(
    "edxapp-ses-domain-identity-verification-resource",
    domain=edxapp_ses_domain_identity.id,
    opts=ResourceOptions(depends_on=[edxapp_ses_verification_record]),
)
edxapp_mail_from = ses.MailFrom(
    "edxapp-ses-mail-from-domain",
    domain=edxapp_ses_domain_identity_verification.domain,
    mail_from_domain=edxapp_ses_domain_identity_verification.domain.apply(
        "bounce.{}".format
    ),
)
# Example Route53 MX record
edxapp_ses_domain_mail_from_mx = route53.Record(
    f"edxapp-ses-mail-from-mx-record-for-{env_name}",
    zone_id=edxapp_zone_id,
    name=edxapp_mail_from.mail_from_domain,
    type="MX",
    ttl=FIVE_MINUTES,
    records=["10 feedback-smtp.us-east-1.amazonses.com"],
)
ses_domain_mail_from_txt = route53.Record(
    "edxapp-ses-domain-mail-from-text-record",
    zone_id=edxapp_zone_id,
    name=edxapp_mail_from.mail_from_domain,
    type="TXT",
    ttl=FIVE_MINUTES,
    records=["v=spf1 include:amazonses.com -all"],
)
edxapp_ses_domain_dkim = ses.DomainDkim(
    "edxapp-ses-domain-dkim", domain=edxapp_ses_domain_identity.domain
)
for loop_counter in range(0, 3):
    route53.Record(
        f"edxapp-ses-domain-dkim-record-{loop_counter}",
        zone_id=edxapp_zone_id,
        name=edxapp_ses_domain_dkim.dkim_tokens[loop_counter].apply(
            lambda dkim_name: f"{dkim_name}._domainkey.{edxapp_mail_domain}"
        ),
        type="CNAME",
        ttl=FIVE_MINUTES,
        records=[
            edxapp_ses_domain_dkim.dkim_tokens[loop_counter].apply(
                "{}.dkim.amazonses.com".format
            )
        ],
    )
edxapp_ses_configuration_set = ses.ConfigurationSet(
    "edxapp-ses-configuration-set",
    reputation_metrics_enabled=True,
    sending_enabled=True,
    name=f"edxapp-{env_name}",
)
edxapp_ses_event_destintations = ses.EventDestination(
    "edxapp-ses-event-destination-routing",
    configuration_set_name=edxapp_ses_configuration_set.name,
    enabled=True,
    matching_types=[
        "send",
        "reject",
        "bounce",
        "complaint",
        "delivery",
        "open",
        "click",
        "renderingFailure",
    ],
    cloudwatch_destinations=[
        ses.EventDestinationCloudwatchDestinationArgs(
            default_value="default",
            dimension_name=f"edxapp-{env_name}",
            value_source="emailHeader",
        )
    ],
)

######################
# Secrets Management #
######################
edxapp_vault_mount = vault.Mount(
    "edxapp-vault-generic-secrets-mount",
    path=f"secret-{stack_info.env_prefix}",
    description="Static secrets storage for MITx Online applications and services",
    type="kv",
)
edxapp_secrets = vault.generic.Secret(
    "edxapp-static-secrets",
    path=edxapp_vault_mount.path.apply("{}/edxapp".format),
    data_json=edxapp_config.require_secret_object("edxapp_secrets").apply(json.dumps),
)
forum_secrets = vault.generic.Secret(
    "edx-forum-static-secrets",
    path=edxapp_vault_mount.path.apply("{}/edx-forum".format),
    data_json=edxapp_config.require_secret_object("edx_forum_secrets").apply(
        json.dumps
    ),
)

# Vault policy definition
edxapp_vault_policy = vault.Policy(
    "edxapp-vault-policy",
    name="edxapp",
    policy=Path(__file__).parent.joinpath("edxapp_policy.hcl").read_text(),
)
# Register edX Platform AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "edxapp-web-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="edxapp-web",
    inferred_entity_type="ec2_instance",
    inferred_aws_region="us-east-1",
    bound_iam_instance_profile_arns=[edxapp_instance_profile.arn],
    bound_ami_ids=[edxapp_web_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[edxapp_vpc_id],
    token_policies=[edxapp_vault_policy.name],
)

vault.aws.AuthBackendRole(
    "edxapp-worker-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="edxapp-worker",
    inferred_entity_type="ec2_instance",
    inferred_aws_region="us-east-1",
    bound_iam_instance_profile_arns=[edxapp_instance_profile.arn],
    bound_ami_ids=[edxapp_worker_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[edxapp_vpc_id],
    token_policies=[edxapp_vault_policy.name],
)

##########################
#     EC2 Deployment     #
##########################

# Create load balancer for Edxapp web nodes
edxapp_web_tag = f"edxapp-web-{env_name}"
edxapp_worker_tag = f"edxapp-worker-{env_name}"
web_lb = lb.LoadBalancer(
    "edxapp-web-load-balancer",
    name=edxapp_web_tag,
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=edxapp_vpc["subnet_ids"],
    security_groups=[
        edxapp_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": edxapp_web_tag}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32
lms_web_lb_target_group = lb.TargetGroup(
    "edxapp-web-lms-alb-target-group",
    vpc_id=edxapp_vpc_id,
    target_type="instance",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=3,
        timeout=3,
        interval=edxapp_config.get_int("elb_healthcheck_interval") or 10,
        path="/heartbeat",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
    ),
    name_prefix=f"lms-{stack_info.env_suffix}-"[:6],
    tags=aws_config.tags,
)
# Studio has some workflows that are stateful, such as importing and exporting courses
# which requires files to be written and read from the same EC2 instance. This adds
# separate target groups and ALB listener rules to route requests for studio to a target
# group with session stickiness enabled so that these stateful workflows don't fail.
# TMM 2021-07-20
studio_web_lb_target_group = lb.TargetGroup(
    "edxapp-web-studio-alb-target-group",
    vpc_id=edxapp_vpc_id,
    target_type="instance",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=2,
        timeout=3,
        interval=5,
        path="/heartbeat",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
    ),
    stickiness=lb.TargetGroupStickinessArgs(
        type="lb_cookie",
        enabled=True,
    ),
    name_prefix=f"studio-{stack_info.env_suffix}-"[:6],
    tags=aws_config.tags,
)
edxapp_web_acm_cert = acm.Certificate(
    "edxapp-load-balancer-acm-certificate",
    domain_name=edxapp_domains["lms"],
    subject_alternative_names=[
        domain for key, domain in edxapp_domains.items() if key != "lms"
    ],
    validation_method="DNS",
    tags=aws_config.tags,
)

edxapp_acm_cert_validation_records = (
    edxapp_web_acm_cert.domain_validation_options.apply(
        partial(acm_certificate_validation_records, zone_id=edxapp_zone_id)
    )
)

edxapp_web_acm_validated_cert = acm.CertificateValidation(
    "wait-for-edxapp-acm-cert-validation",
    certificate_arn=edxapp_web_acm_cert.arn,
    validation_record_fqdns=edxapp_acm_cert_validation_records.apply(
        lambda validation_records: [
            validation_record.fqdn for validation_record in validation_records
        ]
    ),
)
edxapp_web_alb_listener = lb.Listener(
    "edxapp-web-alb-listener",
    certificate_arn=edxapp_web_acm_validated_cert.certificate_arn,
    load_balancer_arn=web_lb.arn,
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=lms_web_lb_target_group.arn,
        )
    ],
    opts=ResourceOptions(delete_before_replace=True),
)
edxapp_studio_web_alb_listener_rule = lb.ListenerRule(
    "edxapp-web-studio-alb-listener-routing",
    listener_arn=edxapp_web_alb_listener.arn,
    actions=[
        lb.ListenerRuleActionArgs(
            type="forward",
            target_group_arn=studio_web_lb_target_group.arn,
        )
    ],
    conditions=[
        lb.ListenerRuleConditionArgs(
            host_header=lb.ListenerRuleConditionHostHeaderArgs(
                values=[edxapp_domains["studio"]]
            )
        )
    ],
    priority=1,
    tags=aws_config.tags,
)
edxapp_lms_web_alb_listener_rule = lb.ListenerRule(
    "edxapp-web-lms-alb-listener-routing",
    listener_arn=edxapp_web_alb_listener.arn,
    actions=[
        lb.ListenerRuleActionArgs(
            type="forward",
            target_group_arn=lms_web_lb_target_group.arn,
        )
    ],
    conditions=[
        lb.ListenerRuleConditionArgs(
            host_header=lb.ListenerRuleConditionHostHeaderArgs(
                values=[edxapp_domains["lms"]]
            )
        )
    ],
    priority=2,
    tags=aws_config.tags,
)

# Create auto scale group and launch configs for Edxapp web and worker
cloud_init_user_data = base64.b64encode(
    "#cloud-config\n{}".format(
        yaml.dump(
            {
                "write_files": [
                    {
                        "path": "/etc/consul.d/99-autojoin.json",
                        "content": json.dumps(
                            {
                                "retry_join": [
                                    "provider=aws tag_key=consul_env "
                                    f"tag_value={env_name}"
                                ],
                                "datacenter": env_name,
                            }
                        ),
                        "owner": "consul:consul",
                    },
                    {
                        "path": "/etc/default/vector",
                        "content": textwrap.dedent(
                            f"""\
                        ENVIRONMENT={env_name}
                        VECTOR_CONFIG_DIR=/etc/vector/
                        """
                        ),  # noqa: WPS355
                        "owner": "root:root",
                    },
                ]
            },
            sort_keys=True,
        )
    ).encode("utf8")
).decode("utf8")

web_instance_type = (
    edxapp_config.get("web_instance_type") or InstanceTypes.high_mem_regular.name
)
web_launch_config = ec2.LaunchTemplate(
    "edxapp-web-launch-template",
    name_prefix=f"edxapp-web-{env_name}-",
    description=f"Launch template for deploying Edxapp web nodes in {env_name}",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=edxapp_instance_profile.arn,
    ),
    image_id=edxapp_web_ami.id,
    vpc_security_group_ids=[
        edxapp_security_group.id,
        edxapp_vpc["security_groups"]["web"],
        consul_security_groups["consul_agent"],
    ],
    instance_type=InstanceTypes[web_instance_type].value,
    key_name=SSH_ACCESS_KEY_NAME,
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": edxapp_web_tag}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": edxapp_web_tag}),
        ),
    ],
    tags=aws_config.tags,
    user_data=cloud_init_user_data,
)
web_asg = autoscaling.Group(
    "edxapp-web-autoscaling-group",
    desired_capacity=edxapp_config.get_int("web_node_capacity")
    or MIN_WEB_NODES_DEFAULT,
    min_size=edxapp_config.get("min_web_nodes") or MIN_WEB_NODES_DEFAULT,
    max_size=edxapp_config.get("max_web_nodes") or MAX_WEB_NODES_DEFAULT,
    health_check_type="ELB",
    vpc_zone_identifiers=edxapp_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=web_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50  # noqa: WPS432
        ),
    ),
    target_group_arns=[lms_web_lb_target_group.arn, studio_web_lb_target_group.arn],
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.tags.items()
    ],
)

worker_instance_type = (
    edxapp_config.get("worker_instance_type") or InstanceTypes.large.name
)
worker_launch_config = ec2.LaunchTemplate(
    "edxapp-worker-launch-template",
    name_prefix=f"{edxapp_worker_tag}-",
    description="Launch template for deploying Edxapp worker nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=edxapp_instance_profile.arn,
    ),
    image_id=edxapp_worker_ami.id,
    block_device_mappings=[
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name="/dev/xvda",
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=edxapp_config.get_int("worker_disk_size")
                or 25,  # noqa: WPS432
                volume_type=DiskTypes.ssd,
            ),
        )
    ],
    vpc_security_group_ids=[
        edxapp_security_group.id,
        consul_security_groups["consul_agent"],
    ],
    instance_type=InstanceTypes[worker_instance_type].value,
    key_name=SSH_ACCESS_KEY_NAME,
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": edxapp_worker_tag}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": edxapp_worker_tag}),
        ),
    ],
    tags=aws_config.tags,
    user_data=cloud_init_user_data,
)
worker_asg = autoscaling.Group(
    "edxapp-worker-autoscaling-group",
    desired_capacity=edxapp_config.get_int("worker_node_capacity") or 1,
    min_size=1,
    max_size=50,  # noqa: WPS432
    health_check_type="EC2",
    vpc_zone_identifiers=edxapp_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=worker_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50  # noqa: WPS432
        ),
        triggers=["tag"],
    ),
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.tags.items()
    ],
)

# Create Route53 DNS records for Edxapp web nodes
for domain_key, domain_value in edxapp_domains.items():
    route53.Record(
        f"edxapp-web-{domain_key}-dns-record",
        name=domain_value,
        type="CNAME",
        ttl=FIVE_MINUTES,
        records=[web_lb.dns_name],
        zone_id=edxapp_zone_id,
    )


export(
    f"{stack_info.env_prefix}_edxapp",
    {
        "mariadb": edxapp_db.db_instance.address,
        "redis": edxapp_redis_cache.address,
        "mfe_bucket": mfe_bucket_name,
        "load_balancer": {"dns_name": web_lb.dns_name, "arn": web_lb.arn},
        "ses_configuration_set": edxapp_ses_configuration_set.name,
    },
)
