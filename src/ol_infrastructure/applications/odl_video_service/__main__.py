"""The complete state needed to provision OVS running on Docker."""

import json
from pathlib import Path

import pulumi_consul as consul
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Alias, Config, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_caller_identity, iam

from bridge.lib.constants import ECR_PULLTHROUGH_CACHE_PREFIX
from bridge.lib.magic_numbers import (
    AWS_RDS_DEFAULT_DATABASE_CAPACITY,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    DEFAULT_REDIS_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache, OLAmazonRedisConfig
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.components.aws.mediaconvert import (
    MediaConvertConfig,
    OLMediaConvert,
)
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.consul import consul_key_helper, get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

# Configuration items and initialziations
if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
ovs_config = Config("ovs")
vault_config = Config("vault")
stack_info = parse_stack()

aws_account = get_caller_identity()

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.apps.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")

target_vpc_name = ovs_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)
target_vpc_id = target_vpc["id"]
data_vpc = network_stack.require_output("data_vpc")

mitodl_zone_id = dns_stack.require_output("odl_zone_id")

# We will take the entire secret structure and load it into Vault as is under
# the root mount further down in the file.
secrets = read_yaml_secrets(
    Path(f"odl_video_service/data.{stack_info.env_suffix}.yaml")
)

aws_config = AWSBase(
    tags={
        "OU": "odl-video",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
)
consul_provider = get_consul_provider(stack_info)
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"

# EKS Related configurations
ovs_namespace = "odl-video-service"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(ovs_namespace, ns)
)

k8s_global_labels = {
    "ol.mit.edu/stack": stack_info.full_name,
    "ol.mit.edu/application": "odl-video-service",
}
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# IAM and instance profile
parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "UNKNOWN_ACTION": {"ignore_locations": []},
    "RESOURCE_EFFECTIVELY_STAR": {"ignore_locations": []},
}

# Get the standard MediaConvert policy statements
mediaconvert_policy_statements = OLMediaConvert.get_standard_policy_statements(
    stack_info.env_suffix, service_name="odl-video"
)

ovs_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Action": ["s3:ListBucket", "s3:HeadObject", "s3:GetObject"],
            "Effect": "Allow",
            "Resource": [
                "arn:aws:s3:::ttv_videos",
                "arn:aws:s3:::ttv_videos/*",
                "arn:aws:s3:::ttv_static",
                "arn:aws:s3:::ttv_static/*",
            ],
        },
        {
            "Action": [
                "elastictranscoder:CancelJob",
                "elastictranscoder:ReadJob",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:job/*"
            ],
        },
        {
            "Action": [
                "elastictranscoder:ListJobsByPipeline",
                "elastictranscoder:ReadPipeline",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:pipeline/{secrets['misc']['et_pipeline_id']}"
            ],
        },
        {
            "Action": [
                "elastictranscoder:CreateJob",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:preset/*",
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:pipeline/{secrets['misc']['et_pipeline_id']}",
            ],
        },
        {
            "Action": [
                "elastictranscoder:ReadPreset",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:elastictranscoder:{aws_config.region}:{aws_account.id}:preset/*",
            ],
        },
        # This block against odl-video-service* buckets is REQUIRED
        # App does not work without it?????
        # TODO MAD 20221115 Why is it required?  # noqa: FIX002, TD002, TD003, TD004
        # The S3 permissions block following this SHOULD cover what this provides
        # but the app must be making some kind of call to bucket that isn't qualified
        # by the environment (CI,RC,Production)
        # There are 21 odl-video-service* buckets at the moment. (JFC...)
        {
            "Action": [
                "s3:HeadObject",
                "s3:GetObject",
                "s3:ListAllMyBuckets",
                "s3:ListBucket",
                "s3:ListObjects",
                "s3:PutObject",
                "s3:DeleteObject",
            ],
            "Effect": "Allow",
            "Resource": [
                "arn:aws:s3:::odl-video-service*",
                "arn:aws:s3:::odl-video-service*/*",
            ],
        },
        {
            "Action": ["sns:ListSubscriptionsByTopic", "sns:Publish"],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:sns:{aws_config.region}:{aws_account.id}:odl-video-service"
            ],
        },
        {
            "Action": [
                "s3:DeleteObject",
                "s3:DeleteObjectVersion",
                "s3:GetAccelerateConfiguration",
                "s3:GetBucketAcl",
                "s3:GetBucketCORS",
                "s3:GetBucketLocation",
                "s3:GetBucketLogging",
                "s3:GetBucketNotification",
                "s3:GetBucketPolicy",
                "s3:GetBucketTagging",
                "s3:GetBucketVersioning",
                "s3:GetBucketWebsite",
                "s3:GetLifecycleConfiguration",
                "s3:GetObject",
                "s3:GetObjectAcl",
                "s3:GetObjectTagging",
                "s3:GetObjectTorrent",
                "s3:GetObjectVersion",
                "s3:GetObjectVersionAcl",
                "s3:GetObjectVersionTagging",
                "s3:GetObjectVersionTorrent",
                "s3:GetReplicationConfiguration",
                "s3:HeadObject",
                "s3:ListAllMyBuckets",
                "s3:ListObjects",
                "s3:ListBucket",
                "s3:ListBucketMultipartUploads",
                "s3:ListBucketVersions",
                "s3:ListMultipartUploadParts",
                "s3:PutBucketWebsite",
                "s3:PutObject",
                "s3:PutObjectTagging",
                "s3:ReplicateDelete",
                "s3:ReplicateObject",
                "s3:RestoreObject",
            ],
            "Effect": "Allow",
            "Resource": [
                f"arn:aws:s3:::{ovs_config.get('s3_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_subtitle_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_thumbnail_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_transcode_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_watch_bucket_name')}/",
                f"arn:aws:s3:::{ovs_config.get('s3_bucket_name')}/*",
                f"arn:aws:s3:::{ovs_config.get('s3_subtitle_bucket_name')}/*",
                f"arn:aws:s3:::{ovs_config.get('s3_thumbnail_bucket_name')}/*",
                f"arn:aws:s3:::{ovs_config.get('s3_transcode_bucket_name')}/*",
                f"arn:aws:s3:::{ovs_config.get('s3_watch_bucket_name')}/*",
            ],
        },
        # Include standard MediaConvert policy statements
        *mediaconvert_policy_statements,
    ],
}
ovs_policy = iam.Policy(
    "odl-video-service-server-policy",
    name_prefix="odl-video-service-server-policy-",
    path=f"/ol-applications/odl-video-service-server/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        ovs_policy_document,
        stringify=True,
        parliament_config=parliament_config,
    ),
    description=(
        "AWS access permissions to allow odl-video-service to s3 buckets and transcode"
        " services."
    ),
)


# Ultimately this trust role may not actually serve any purpose because the app
# has to have an aws secret key. See below.
ovs_service_account_name = "odl-video-service"
ovs_trust_role = OLEKSTrustRole(
    f"odl-video-service-trust-role-{stack_info.env_suffix}",
    role_config=OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=f"applications-{stack_info.name}",
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        description="Trust role for allowing the ovs service account to "
        "access the aws API",
        policy_operator="StringEquals",
        role_name="odl-video-service",
        service_account_identifier=f"system:serviceaccount:{ovs_namespace}:{ovs_service_account_name}",
        tags=aws_config.tags,
    ),
    opts=ResourceOptions(
        depends_on=[ovs_policy],
    ),
)
iam.RolePolicyAttachment(
    f"odl-video-service-s3-transcode-role-policy-{stack_info.env_suffix}",
    policy_arn=ovs_policy.arn,
    role=ovs_trust_role.role.name,
    opts=ResourceOptions(
        aliases=[Alias(f"odl-video-service-server-s3-transcode-role-policy-{env_name}")]
    ),
)
ovs_service_account = kubernetes.core.v1.ServiceAccount(
    f"odl-video-service-service-account-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=ovs_service_account_name,
        namespace=ovs_namespace,
        annotations={
            "eks.amazonaws.com/role-arn": ovs_trust_role.role.arn,
        },
    ),
    automount_service_account_token=False,
)

# Need to pin the same policy that the trustrole profile will use to the aws auth
# backend because the app still needs an AWS Key
ocw_studio_vault_backend_role = vault.aws.SecretBackendRole(
    f"ovs-server-{stack_info.env_suffix}",
    name="ovs-server",
    backend="aws-mitx",
    credential_type="iam_user",
    iam_tags={"OU": "operations", "vault_managed": "True"},
    policy_arns=[
        policy_stack.require_output("iam_policies")["describe_instances"],
        ovs_policy.arn,
    ],
    opts=ResourceOptions(delete_before_replace=True),
)

############################################################
# Network Access Control
# Create various security groups
ovs_app_security_group = ec2.SecurityGroup(
    f"odl-video-service-server-security-group-{env_name}",
    name=f"odl-video-service-server-{target_vpc_name}-{env_name}",
    description="Access control for odl-video-service servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                "Allow traffic to the odl-video-service server on port"
                f" {DEFAULT_HTTPS_PORT}"
            ),
        ),
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_HTTP_PORT,
            to_port=DEFAULT_HTTP_PORT,
            cidr_blocks=["0.0.0.0/0"],
            description=(
                "Allow traffic to the odl-video-service server on port"
                f" {DEFAULT_HTTP_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

ovs_database_security_group = ec2.SecurityGroup(
    f"odl-video-service-database-security-group-{env_name}",
    name=f"odl-video-service-database-{target_vpc_name}-{env_name}",
    description="Access control for the odl-video-service database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                ovs_app_security_group.id,
                consul_stack.require_output("security_groups")["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
                data_vpc["security_groups"]["integrator"],
            ],
            cidr_blocks=data_vpc["k8s_pod_subnet_cidrs"].apply(
                lambda pod_cidrs: [*pod_cidrs, target_vpc["cidr"]]
            ),
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description=(
                "Access to Postgres from odl-video-service nodes on"
                f" {DEFAULT_POSTGRES_PORT}"
            ),
        ),
    ],
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

ovs_redis_security_group = ec2.SecurityGroup(
    f"odl-video-service-redis-security-group-{env_name}",
    name=f"odl-video-service-redis-{target_vpc_name}-{env_name}",
    description="Access control for the odl-video-service redis queue",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                ovs_app_security_group.id,
            ],
            protocol="tcp",
            from_port=DEFAULT_REDIS_PORT,
            to_port=DEFAULT_REDIS_PORT,
            description=(
                f"Access to Redis from odl-video-service nodes on {DEFAULT_REDIS_PORT}"
            ),
        ),
    ],
    egress=default_egress_args,
    vpc_id=target_vpc_id,
    tags=aws_config.tags,
)

############################################################
# Database
rds_defaults = defaults(stack_info)["rds"]
rds_password = ovs_config.require("rds_password")
ovs_db_config = OLPostgresDBConfig(
    instance_name=f"odl-video-service-{stack_info.env_suffix}",
    password=rds_password,
    storage=ovs_config.get("db_capacity") or str(AWS_RDS_DEFAULT_DATABASE_CAPACITY),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[ovs_database_security_group],
    engine_major_version="15",
    parameter_overrides=[],
    db_name="odlvideo",
    tags=aws_config.tags,
    **rds_defaults,
)
ovs_db = OLAmazonDB(ovs_db_config)

db_address = ovs_db.db_instance.address
db_port = ovs_db.db_instance.port

ovs_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=ovs_db_config.db_name,
    mount_point=f"{ovs_db_config.engine}-odl-video-service",
    db_admin_username=ovs_db_config.username,
    db_admin_password=rds_password,
    db_host=db_address,
)
ovs_db_vault_backend = OLVaultDatabaseBackend(ovs_db_vault_backend_config)

############################################################
# Redis
redis_auth_token = secrets["redis"]["auth_token"]
redis_config = Config("redis")
ovs_server_redis_config = OLAmazonRedisConfig(
    encrypt_transit=True,
    auth_token=redis_auth_token,
    engine_version="6.2",
    num_instances=3,
    shard_count=1,
    auto_upgrade=True,
    cluster_mode_enabled=False,
    cluster_description="Redis cluster for ODL Video Service.",
    cluster_name=f"odl-video-service-redis-{stack_info.env_suffix}",
    security_groups=[ovs_redis_security_group.id],
    subnet_group=target_vpc["elasticache_subnet"],
    tags=aws_config.tags,
    **defaults(stack_info)["redis"],
)
ovs_server_redis_cluster = OLAmazonCache(ovs_server_redis_config)

############################################################
# Create vault policy and associate it with the auth backend role
# on the vault k8as cluster auth endpoint
ovs_vault_policy = vault.Policy(
    f"odl-video-service-vault-policy-{stack_info.env_suffix}",
    name="odl-video-service",
    policy=Path(__file__)
    .parent.joinpath("odl_video_service_server_policy.hcl")  # TODO FIX
    .read_text(),
    opts=ResourceOptions(aliases=[Alias("ovs-server-vault-policy")]),
)

ovs_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
    "odl-video-service-k8s-auth-backend-role",
    role_name="odl-video-service",
    backend=cluster_stack.require_output("vault_auth_endpoint"),
    bound_service_account_names=["*"],
    bound_service_account_namespaces=[ovs_namespace],
    token_policies=[ovs_vault_policy.name],
)

vault_k8s_resources = OLVaultK8SResources(
    resource_config=OLVaultK8SResourcesConfig(
        application_name="odl-video-service",
        namespace=ovs_namespace,
        labels=k8s_global_labels,
        vault_address=vault_config.require("address"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        vault_auth_role_name=ovs_vault_auth_backend_role.role_name,
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[ovs_vault_auth_backend_role],
    ),
)

# Vault KV2 mount definition
ovs_server_vault_mount = vault.Mount(
    "ovs-server-configuration-secrets-mount",
    path="secret-odl-video-service",
    type="kv-v2",
    options={"version": 2},
    description=(
        "Storage of configuration credentials and secrets used by odl-video-service"
    ),
    opts=ResourceOptions(delete_before_replace=True),
)


ovs_server_secrets = vault.generic.Secret(
    "ovs-server-configuration-secrets",
    path=ovs_server_vault_mount.path.apply("{}/ovs-secrets".format),
    data_json=json.dumps(secrets),
)
enabled_annotations = ovs_config.get_bool("feature_annotations")
use_shibboleth = ovs_config.get_bool("use_shibboleth")
if use_shibboleth:
    nginx_config_file_path = "/etc/nginx/nginx_with_shib.conf"
else:
    nginx_config_file_path = "/etc/nginx/nginx_wo_shib.conf"

domains_string = ",".join(ovs_config.get_object("domains"))

# Create Route53 DNS records
# five_minutes = 60 * 5
# for domain in ovs_config.get_object("route53_managed_domains"):
#    route53.Record(
#        f"ovs-server-dns-record-{domain}",
#        name=domain,
#        type="CNAME",
#        ttl=five_minutes,
#        records=[autoscale_setup.load_balancer.dns_name],
#        zone_id=mitodl_zone_id,
#    )

ovs_mediaconvert_config = MediaConvertConfig(
    service_name="odl-video",
    env_suffix=stack_info.env_suffix,
    tags=aws_config.tags,
    policy_arn=ovs_policy.arn,
    host=ovs_config.get("default_domain"),
)

ovs_mediaconvert = OLMediaConvert(ovs_mediaconvert_config)

consul_keys = {
    "ovs/database_endpoint": db_address,
    "ovs/default_domain": ovs_config.get("default_domain"),
    "ovs/domains": domains_string,
    "ovs/edx_base_url": ovs_config.get("edx_base_url"),
    "ovs/environment": stack_info.env_suffix,
    "ovs/feature_annotations": ("True" if enabled_annotations else "False"),
    "ovs/log_level": ovs_config.get("log_level"),
    "ovs/mediaconvert_sns_topic_arn": ovs_mediaconvert.sns_topic.arn,
    "ovs/nginx_config_file_path": nginx_config_file_path,
    "ovs/redis_cluster_address": ovs_server_redis_cluster.address,
    "ovs/redis_max_connections": redis_config.get("max_connections") or 65000,
    "ovs/s3_bucket_name": ovs_config.get("s3_bucket_name"),
    "ovs/s3_subtitle_bucket_name": ovs_config.get("s3_subtitle_bucket_name"),
    "ovs/s3_thumbnail_bucket_name": ovs_config.get("s3_thumbnail_bucket_name"),
    "ovs/s3_transcode_bucket_name": ovs_config.get("s3_transcode_bucket_name"),
    "ovs/s3_watch_bucket_name": ovs_config.get("s3_watch_bucket_name"),
    "ovs/use_shibboleth": "True" if use_shibboleth else "False",
}
consul.Keys(
    "ovs-server-configuration-data",
    keys=consul_key_helper(consul_keys),
    opts=consul_provider,
)

volumes = []
nginx_volume_mounts = []
app_volume_mounts = []


# Application deployment
application_labels = k8s_global_labels | {
    "ol.mit.edu/service": "webapp",
}
ovs_k8s_deployment_resource = kubernetes.apps.v1.Deployment(
    f"odl-video-service-k8s-deployment-resource-{env_name}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="odl-video-service",
        namespace=ovs_namespace,
        labels=application_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=application_labels,
        ),
        strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
            type="RollingUpdate",
            rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                max_surge=0,
                max_unavailable=1,
            ),
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=application_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                volumes=volumes,
                init_containers=[],
                dns_policy="ClusterFirst",
                service_account_name=ovs_service_account_name,
                containers=[
                    # Nginx + shib container
                    kubernetes.core.v1.ContainerArgs(
                        name="nginx",
                        image=f"{ECR_PULLTHROUGH_CACHE_PREFIX}/pennlabs/shibboleth-sp-nginx:latest",
                        ports=[
                            kubenetes.core.v1.ContainerPortArgs(
                                container_port=DEFAULT_HTTP_PORT,
                            ),
                            kubenetes.core.v1.ContainerPortArgs(
                                container_port=DEFAULT_HTTPS_PORT,
                            ),
                        ],
                        image_pull_policy="IfNotPresent",
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "100m",
                                "memory": "128Mi",
                            },
                            limits={
                                "cpu": "200m",
                                "memory": "256Mi",
                            },
                        ),
                        volume_mounts=nginx_volume_mounts,
                    ),
                    kubernetes.core.v1.ContainerArgs(
                        name="ovs-webapp",
                        image=f"{ECR_PULLTHROUGH_CACHE_PREFIX}/mitodl/ovs-app:${OVS_DOCKER_TAG}",
                    ),
                ],
            ),
        ),
    ),
)

# Add the resources to the export
export(
    "odl_video_service",
    {
        "rds_host": db_address,
        "redis_cluster": ovs_server_redis_cluster.address,
        "mediaconvert_queue": ovs_mediaconvert.queue.id,
    },
)
