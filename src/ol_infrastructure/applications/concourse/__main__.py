"""Create the resources needed to run a Concourse CI/CD system.

- Launch a PostgreSQL instance in RDS
- Create the IAM policies needed to grant the needed access for various build pipelines
- Create an autoscaling group for Concourse web nodes
- Create an autoscaling group for Concourse worker instances
"""

import base64
import importlib
import json
import textwrap
from functools import partial
from pathlib import Path

import pulumi_vault as vault
import yaml
from pulumi import Config, Output, StackReference, export
from pulumi_aws import ec2, get_caller_identity, iam, route53
from pulumi_consul import Node, Service, ServiceCheckArgs

from bridge.lib.magic_numbers import (
    CONCOURSE_WORKER_HEALTHCHECK_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    MAXIMUM_PORT_NUMBER,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.aws.auto_scale_group import (
    BlockDeviceMapping,
    OLAutoScaleGroupConfig,
    OLAutoScaling,
    OLLaunchTemplateConfig,
    OLLoadBalancerConfig,
    OLTargetGroupConfig,
    SpotInstanceOptions,
    TagSpecification,
)
from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import (
    DiskTypes,
    InstanceTypes,
    default_egress_args,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import setup_vault_provider

##################################
#     Setup + Config Retrival    #
##################################

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()
concourse_config = Config("concourse")
stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(f"infrastructure.consul.operations.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

target_vpc_name = concourse_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
target_vpc = network_stack.require_output(target_vpc_name)

consul_security_groups = consul_stack.require_output("security_groups")
aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"operations-{stack_info.env_suffix}"}
)
aws_account = get_caller_identity()
ops_vpc_id = target_vpc["id"]
concourse_web_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["concourse-web-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

concourse_worker_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="name", values=["concourse-worker-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)
concourse_web_tag = f"concourse-web-{stack_info.env_suffix}"
consul_provider = get_consul_provider(stack_info)

grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)


def build_worker_user_data(
    concourse_team: str, concourse_tags: list[str], consul_dc: str
) -> str:
    yaml_contents = {
        "write_files": [
            {
                "path": "/etc/consul.d/02-autojoin.json",
                "content": json.dumps(
                    {
                        "retry_join": [
                            f"provider=aws tag_key=consul_env tag_value={consul_dc}"  # noqa: ISC001, RUF100
                        ],
                        "datacenter": consul_dc,
                    }
                ),
                "owner": "consul:consul",
            },
            {
                "path": "/etc/default/vector",
                "content": textwrap.dedent(
                    f"""\
                    ENVIRONMENT={consul_dc}
                    APPLICATION=concourse-worker
                    SERVICE=concourse
                    VECTOR_CONFIG_DIR=/etc/vector/
                    VECTOR_STRICT_ENV_VARS=false
                    AWS_REGION={aws_config.region}
                    GRAFANA_CLOUD_API_KEY={grafana_credentials["api_key"]}
                    GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials["prometheus_user_id"]}
                    GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials["loki_user_id"]}
                    """
                ),
                "owner": "root:root",
            },
        ]
    }
    if concourse_team:
        yaml_contents["write_files"].append(
            {
                "path": "/etc/default/concourse-team",
                "content": textwrap.dedent(
                    f"""\
                     CONCOURSE_TEAM={concourse_team}
                     """
                ),
                "owner": "root:root",
            }
        )
    if concourse_tags:
        yaml_contents["write_files"].append(
            {
                "path": "/etc/default/concourse-tags",
                "content": f"CONCOURSE_TAG={','.join(concourse_tags)}",
                "owner": "root:root",
            }
        )
    return base64.b64encode(
        "#cloud-config\n{}".format(
            yaml.dump(
                yaml_contents,
                sort_keys=True,
            )
        ).encode("utf8")
    ).decode("utf8")


###############################
#      General Resources      #
###############################

# IAM and instance profile
concourse_web_instance_role = iam.Role(
    f"concourse-instance-role-{stack_info.env_suffix}",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    path="/ol-applications/concourse/role/",
    tags=aws_config.tags,
)

# Dynamically load IAM policies from the IAM policies module and set them aside
# First deduplicate all policy names into 'iam_policy_names' list.
all_iam_policy_names: list[str] = []
for worker_def in concourse_config.get_object("workers") or {}:
    all_iam_policy_names = all_iam_policy_names + (worker_def["iam_policies"] or [])
iam_policy_names = set(
    all_iam_policy_names + (concourse_config.get_object("web_iam_polcies") or [])
)

iam_policy_objects = {}
for iam_policy in iam_policy_names or []:
    policy_module = importlib.import_module(f"iam_policies.{iam_policy}")
    iam_policy_object = iam.Policy(
        f"cicd-iam-permissions-policy-{iam_policy}-{stack_info.env_suffix}",
        path=f"/ol-infrastructure/iam/cicd-{stack_info.env_suffix}/",
        policy=lint_iam_policy(
            policy_module.policy_definition,
            parliament_config={
                "PERMISSIONS_MANAGEMENT_ACTIONS": {
                    "ignore_locations": [{"actions": ["ec2:modifysnapshotattributte"]}]
                },
                "RESOURCE_STAR": {},
            },
        ),
        name_prefix=f"cicd-policy-{iam_policy}-{stack_info.env_suffix}",
        tags=aws_config.tags,
    )
    iam_policy_objects[iam_policy] = iam_policy_object

# Loop through the policy names hooked to web nodes and attach them
for iam_policy_name in concourse_config.get_object("web_iam_policies") or []:
    iam_policy_object = iam_policy_objects[iam_policy_name]
    iam.RolePolicyAttachment(
        f"concourse-instance-policy-web-policy-{iam_policy_name}-{stack_info.env_suffix}",
        policy_arn=iam_policy_object.arn,
        role=concourse_web_instance_role.name,
    )

# Other, misc IAM policy attachments
iam.RolePolicyAttachment(
    f"concourse-describe-instance-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=concourse_web_instance_role.name,
)

iam.RolePolicyAttachment(
    f"concourse-route53-role-policy-{stack_info.env_suffix}",
    policy_arn=policy_stack.require_output("iam_policies")["route53_odl_zone_records"],
    role=concourse_web_instance_role.name,
)

concourse_instance_profile = iam.InstanceProfile(
    f"concourse-instance-profile-{stack_info.env_suffix}",
    role=concourse_web_instance_role.name,
    path="/ol-applications/concourse/profile/",
)

# Mount Vault secrets backend and populate secrets
concourse_secrets_mount = vault.Mount(
    "concourse-app-secrets",
    description="Generic secrets storage for Concourse application deployment",
    path="secret-concourse",
    type="kv-v2",
)
vault.generic.Secret(
    "concourse-web-secret-values",
    path=concourse_secrets_mount.path.apply(lambda mount_path: f"{mount_path}/web"),
    data_json=Output.secret(
        read_yaml_secrets(Path(f"concourse/operations.{stack_info.env_suffix}.yaml"))[
            "web"
        ]
    ).apply(json.dumps),
)
vault.generic.Secret(
    "concourse-worker-secret-values",
    path=concourse_secrets_mount.path.apply(lambda mount_path: f"{mount_path}/worker"),
    data_json=Output.secret(
        read_yaml_secrets(Path(f"concourse/operations.{stack_info.env_suffix}.yaml"))[
            "worker"
        ]
    ).apply(json.dumps),
)
for pipeline_var_path, secret_data in read_yaml_secrets(
    Path(f"concourse/operations.{stack_info.env_suffix}.yaml")
)["pipelines"].items():
    secret_path = partial("{1}/{0}".format, pipeline_var_path)
    vault.generic.Secret(
        f"concourse-pipeline-credentials-{pipeline_var_path}",
        path=concourse_secrets_mount.path.apply(secret_path),
        data_json=json.dumps(secret_data),
    )

# Vault policy definition
concourse_vault_policy = vault.Policy(
    "concourse-vault-policy",
    name="concourse",
    policy=Path(__file__).parent.joinpath("concourse_policy.hcl").read_text(),
)
# Register Concourse AMI for Vault AWS auth
vault.aws.AuthBackendRole(
    "concourse-web-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    role="concourse-web",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[concourse_instance_profile.arn],
    bound_ami_ids=[concourse_web_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[ops_vpc_id],
    token_policies=[concourse_vault_policy.name],
)

##################################
#     Network Access Control     #
##################################
# Link the two groups web    -> worker (all)
#                     worker -> worker (all)
#                     worker -> web    (all)
#         not needed: web    -> web    (all)

# Create worker node security group
concourse_worker_security_group = ec2.SecurityGroup(
    f"concourse-worker-security-group-{stack_info.env_suffix}",
    name=f"concourse-worker-operations-{stack_info.env_suffix}",
    description="Access control for Concourse worker servers",
    egress=default_egress_args,
    vpc_id=ops_vpc_id,
)

# Create web node security group
concourse_web_security_group = ec2.SecurityGroup(
    f"concourse-web-security-group-{stack_info.env_suffix}",
    name=f"concourse-web-operations-{stack_info.env_suffix}",
    description="Access control for Concourse web servers",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            self=True,
            security_groups=[concourse_worker_security_group.id],
            from_port=0,
            to_port=MAXIMUM_PORT_NUMBER,
            protocol="tcp",
            description="Allow Concourse workers to connect to Concourse web nodes",
        )
    ],
    egress=default_egress_args,
    vpc_id=ops_vpc_id,
)

ec2.SecurityGroupRule(
    "concourse-worker-access-from-concourse-web",
    security_group_id=concourse_worker_security_group.id,
    source_security_group_id=concourse_web_security_group.id,
    protocol="tcp",
    from_port=0,
    to_port=MAXIMUM_PORT_NUMBER,
    description="Allow all traffic from Concourse web nodes to workers",
    type="ingress",
)

ec2.SecurityGroupRule(
    "concourse-worker-peer-ingress",
    security_group_id=concourse_worker_security_group.id,
    type="ingress",
    protocol="tcp",
    self=True,
    from_port=0,
    to_port=MAXIMUM_PORT_NUMBER,
    description=(
        "Allow Concourse workers to connect to all other concourse workers for"
        " p2p streaming."
    ),
)

# Create security group for Concourse Postgres database
concourse_db_security_group = ec2.SecurityGroup(
    f"concourse-db-access-{stack_info.env_suffix}",
    name=f"concourse-db-access-{stack_info.env_suffix}",
    description="Access from Concourse instances to the associated Postgres database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                concourse_web_security_group.id,
                consul_security_groups["consul_server"],
                vault_stack.require_output("vault_server")["security_group"],
            ],
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            description="Access to Postgres from Concourse web nodes",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=ops_vpc_id,
)


##########################
#     Database Setup     #
##########################
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    concourse_config.get("db_instance_size") or rds_defaults["instance_size"]
)
rds_defaults["use_blue_green"] = False
rds_defaults["read_replica"] = None
concourse_db_config = OLPostgresDBConfig(
    instance_name=f"concourse-db-{stack_info.env_suffix}",
    password=concourse_config.require("db_password"),
    storage=concourse_config.get("db_capacity"),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[concourse_db_security_group],
    tags=aws_config.tags,
    db_name="concourse",
    **rds_defaults,
)
concourse_db = OLAmazonDB(concourse_db_config)

concourse_db_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=concourse_db_config.db_name,
    mount_point=f"{concourse_db_config.engine}-concourse",
    db_admin_username=concourse_db_config.username,
    db_admin_password=concourse_config.require("db_password"),
    db_host=concourse_db.db_instance.address,
)
concourse_db_vault_backend = OLVaultDatabaseBackend(concourse_db_vault_backend_config)

concourse_db_consul_node = Node(
    "concourse-instance-db-node",
    name="concourse-postgres-db",
    address=concourse_db.db_instance.address,
    opts=consul_provider,
)

concourse_db_consul_service = Service(
    "concourse-instance-db-service",
    node=concourse_db_consul_node.name,
    name="concourse-postgres",
    port=concourse_db_config.port,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        ServiceCheckArgs(
            check_id="concourse-instance-db",
            interval="10s",
            name="concourse-instance-id",
            timeout="60s",
            status="passing",
            tcp=Output.all(
                address=concourse_db.db_instance.address,
                port=concourse_db.db_instance.port,
            ).apply(lambda db: "{address}:{port}".format(**db)),
        )
    ],
    opts=consul_provider,
)

###################################
#     Web Node EC2 Deployment     #
###################################

# Create load balancer for Concourse web nodes
ol_web_lb_config = OLLoadBalancerConfig(
    subnets=target_vpc["subnet_ids"],
    security_groups=[target_vpc["security_groups"]["web"]],
    tags=aws_config.merged_tags({"Name": concourse_web_tag}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32
ol_web_target_group_config = OLTargetGroupConfig(
    vpc_id=ops_vpc_id,
    health_check_interval=10,
    health_check_healthy_threshold=2,
    health_check_path="/api/v1/info",
    health_check_port=str(DEFAULT_HTTPS_PORT),
    health_check_protocol="HTTPS",
    tags=aws_config.tags,
)

# Create auto scale group and launch configs for Concourse web and worker
web_instance_type = (
    concourse_config.get("web_instance_type") or InstanceTypes.burstable_medium.name
)
consul_datacenter = consul_stack.require_output("datacenter")

ol_web_launch_config = OLLaunchTemplateConfig(
    block_device_mappings=[
        BlockDeviceMapping(
            volume_size=(concourse_config.get_int("web_disk_size") or 25)
        )
    ],
    image_id=concourse_web_ami.id,
    instance_type=InstanceTypes[web_instance_type].value,
    instance_profile_arn=concourse_instance_profile.arn,
    security_groups=[
        concourse_web_security_group.id,
        target_vpc["security_groups"]["web"],
        consul_security_groups["consul_agent"],
    ],
    tags=aws_config.tags,
    tag_specifications=[
        TagSpecification(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": concourse_web_tag}),
        ),
        TagSpecification(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": concourse_web_tag}),
        ),
    ],
    user_data=consul_datacenter.apply(
        lambda consul_dc: base64.b64encode(
            "#cloud-config\n{}".format(
                yaml.dump(
                    {
                        "write_files": [
                            {
                                "path": "/etc/consul.d/02-autojoin.json",
                                "content": json.dumps(
                                    {
                                        "retry_join": [
                                            "provider=aws tag_key=consul_env "
                                            f"tag_value={consul_dc}"
                                        ],
                                        "datacenter": consul_dc,
                                    }
                                ),
                                "owner": "consul:consul",
                            },
                            {
                                "path": "/etc/default/traefik",
                                "content": "DOMAIN={}".format(
                                    concourse_config.require("web_host_domain")
                                ),
                            },
                            {
                                "path": "/etc/default/vector",
                                "content": textwrap.dedent(
                                    f"""\
                                    ENVIRONMENT={consul_dc}
                                    APPLICATION=concourse-web
                                    SERVICE=concourse
                                    VECTOR_CONFIG_DIR=/etc/vector/
                                    VECTOR_STRICT_ENV_VARS=false
                                    AWS_REGION={aws_config.region}
                                    GRAFANA_CLOUD_API_KEY={grafana_credentials["api_key"]}
                                    GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials["prometheus_user_id"]}
                                    GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials["loki_user_id"]}
                                    """
                                ),
                                "owner": "root:root",
                            },
                            {
                                "path": "/etc/default/alloy",
                                "content": textwrap.dedent(
                                    f"""
                                    CONFIG_FILE="/etc/alloy/config.alloy"
                                    CUSTOM_ARGS=""
                                    RESTART_ON_UPGRADE=true
                                    GRAFANA_LOKI_ENDPOINT="{grafana_credentials["loki_endpoint"]}"
                                    GRAFANA_LOKI_PASSWORD="{grafana_credentials["loki_api_key"]}"
                                    GRAFANA_LOKI_USER="{grafana_credentials["loki_user_id"]}"
                                    GRAFANA_MIMIR_ENDPOINT="{grafana_credentials["prometheus_endpoint"]}"
                                    GRAFANA_MIMIR_PASSWORD="{grafana_credentials["prometheus_api_key"]}"
                                    GRAFANA_MIMIR_USERNAME="{grafana_credentials["prometheus_user_id"]}"
                                    GRAFANA_TEMPO_ENDPOINT="{grafana_credentials["tempo_endpoint"]}"
                                    GRAFANA_TEMPO_PASSWORD="{grafana_credentials["tempo_api_key"]}"
                                    GRAFANA_TEMPO_USERNAME="{grafana_credentials["tempo_user_id"]}"
                                    """
                                ),
                                "owner": "root:root",
                            },
                        ]
                    },
                    sort_keys=True,
                )
            ).encode("utf8")
        ).decode("utf8")
    ),
)

web_asg_config = concourse_config.get_object("web_auto_scale")

ol_web_asg_config = OLAutoScaleGroupConfig(
    asg_name=f"concourse-web-{stack_info.env_suffix}",
    aws_config=aws_config,
    desired_size=web_asg_config["desired"] or 1,
    min_size=web_asg_config["min"] or 1,
    max_size=web_asg_config["max"] or 5,
    max_instance_lifetime_seconds=web_asg_config["max_instance_lifetime_seconds"],
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    tags=aws_config.merged_tags({"Name": concourse_web_tag}),
)

ol_web_as_setup = OLAutoScaling(
    lb_config=ol_web_lb_config,
    tg_config=ol_web_target_group_config,
    asg_config=ol_web_asg_config,
    lt_config=ol_web_launch_config,
)


############################################
#     Worker Node IAM + EC2 Deployment     #
############################################
concourse_worker_instance_profiles = []

for worker_def in concourse_config.get_object("workers") or []:
    worker_class_name = worker_def["name"]

    # Create IAM role + attach policies to it
    concourse_worker_instance_role = iam.Role(
        f"concourse-instance-role-worker-{worker_class_name}-{stack_info.env_suffix}",
        assume_role_policy=json.dumps(
            {
                "Version": "2012-10-17",
                "Statement": {
                    "Effect": "Allow",
                    "Action": "sts:AssumeRole",
                    "Principal": {"Service": "ec2.amazonaws.com"},
                },
            }
        ),
        path="/ol-applications/concourse/role/",
        tags=aws_config.tags,  # We will leave all the IAM resources with default tags.
    )

    export(f"{worker_class_name}-instance-role-arn", concourse_worker_instance_role.arn)

    for iam_policy_name in worker_def["iam_policies"] or []:
        iam_policy_object = iam_policy_objects[iam_policy_name]
        iam.RolePolicyAttachment(
            f"concourse-instance-policy-worker-{worker_class_name}-policy-{iam_policy_name}-{stack_info.env_suffix}",
            policy_arn=iam_policy_object.arn,
            role=concourse_worker_instance_role.name,
        )

    concourse_worker_instance_profile = iam.InstanceProfile(
        f"concourse-instance-profile-worker-{worker_class_name}-{stack_info.env_suffix}",
        role=concourse_worker_instance_role.name,
        path="/ol-applications/concourse/profile/",
    )

    concourse_worker_instance_profiles.append(concourse_worker_instance_profile)

    # Create EC2 resources
    build_worker_user_data_partial = partial(
        build_worker_user_data,
        worker_def.get("concourse_team"),
        worker_def.get("concourse_tags", []),
    )

    worker_instance_type_name = worker_def.get(
        "instance_type", InstanceTypes.burstable_large.name
    )
    worker_instance_type = InstanceTypes.dereference(worker_instance_type_name)

    worker_name_tag = f"concourse-worker-{worker_class_name}-{stack_info.env_suffix}"

    # Configure spot instances for Concourse workers (enabled by default)
    use_spot_instances = worker_def.get("use_spot_instances", True)
    spot_options_config = None
    if use_spot_instances and "spot_options" in worker_def:
        spot_config = worker_def["spot_options"]
        spot_options_config = SpotInstanceOptions(
            max_price=spot_config.get("max_price"),
            spot_instance_type=spot_config.get("spot_instance_type", "one-time"),
            instance_interruption_behavior=spot_config.get(
                "instance_interruption_behavior", "terminate"
            ),
        )

    ol_worker_launch_config = OLLaunchTemplateConfig(
        block_device_mappings=[
            BlockDeviceMapping(
                volume_type=DiskTypes.ssd,  # gp3
                volume_size=worker_def.get("disk_size_gb", 3000),
                throughput=worker_def.get("disk_throughput", 125),
                iops=worker_def.get("disk_iops", 3000),
                delete_on_termination=True,
            )
        ],
        image_id=concourse_worker_ami.id,
        instance_type=worker_instance_type,
        instance_profile_arn=concourse_worker_instance_profile.arn,
        security_groups=[
            concourse_worker_security_group.id,
            consul_security_groups["consul_agent"],
        ],
        tags=aws_config.merged_tags({"Name": worker_name_tag}, worker_def["aws_tags"]),
        tag_specifications=[
            TagSpecification(
                resource_type="instance",
                tags=aws_config.merged_tags(
                    {"Name": worker_name_tag}, worker_def["aws_tags"]
                ),
            ),
            TagSpecification(
                resource_type="volume",
                tags=aws_config.merged_tags(
                    {"Name": worker_name_tag}, worker_def["aws_tags"]
                ),
            ),
        ],
        user_data=consul_datacenter.apply(build_worker_user_data_partial),
        use_spot_instances=use_spot_instances,
        spot_options=spot_options_config,
    )

    # We will create a 'fake' lb, targetgroup and lblistener to make use of
    # aws's ability to do healthchecks and automatically swap out stalled
    # concourse workers.
    # We will never actually interact with workers via these resources.
    ol_worker_lb_config = OLLoadBalancerConfig(
        internal=True,
        subnets=target_vpc["subnet_ids"],
        security_groups=[concourse_worker_security_group.id],
        tags=aws_config.merged_tags({"Name": worker_name_tag}, worker_def["aws_tags"]),
    )
    ol_worker_target_group_config = OLTargetGroupConfig(
        vpc_id=ops_vpc_id,
        health_check_interval=60,
        health_check_healthy_threshold=2,
        health_check_matcher="200",
        health_check_path="/",
        health_check_port=str(CONCOURSE_WORKER_HEALTHCHECK_PORT),
        health_check_protocol="HTTP",
        health_check_timeout=20,
        health_check_unhealthy_threshold=5,
        tags=aws_config.merged_tags({"Name": worker_name_tag}, worker_def["aws_tags"]),
    )

    ol_web_asg_config = OLAutoScaleGroupConfig(
        asg_name=f"concourse-web-{stack_info.env_suffix}",
        aws_config=aws_config,
        desired_size=web_asg_config["desired"] or 1,
        min_size=web_asg_config["min"] or 1,
        max_size=web_asg_config["max"] or 5,
        max_instance_lifetime=web_asg_config.get("max_instance_lifetime_seconds"),
        vpc_zone_identifiers=target_vpc["subnet_ids"],
        tags=aws_config.merged_tags({"Name": concourse_web_tag}),
    )

    auto_scale_config = worker_def["auto_scale"]
    ol_worker_asg_config = OLAutoScaleGroupConfig(
        asg_name=f"concourse-worker-{stack_info.env_suffix}-{worker_class_name}-alb",
        aws_config=aws_config,
        desired_size=auto_scale_config["desired"] or 1,
        min_size=auto_scale_config["min"] or 1,
        max_size=auto_scale_config["max"] or 5,
        max_instance_lifetime_seconds=auto_scale_config[
            "max_instance_lifetime_seconds"
        ],
        vpc_zone_identifiers=target_vpc["subnet_ids"],
        tags=aws_config.merged_tags(
            {"Name": worker_name_tag},
            worker_def["aws_tags"],
            {
                "ami_id": concourse_worker_ami.id,
                "concourse_type": f"worker-{worker_class_name}",
            },
        ),
    )
    ol_worker_as_setup = OLAutoScaling(
        lb_config=ol_worker_lb_config,
        tg_config=ol_worker_target_group_config,
        asg_config=ol_worker_asg_config,
        lt_config=ol_worker_launch_config,
    )


vault.aws.AuthBackendRole(
    "concourse-worker-ami-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[
        iam_instance_profile.arn
        for iam_instance_profile in concourse_worker_instance_profiles
    ],
    role="concourse-worker",
    bound_ami_ids=[concourse_worker_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[ops_vpc_id],
    token_policies=[concourse_vault_policy.name],
)

# Vault policy definition
concourse_vault_resource_policy = vault.Policy(
    "concourse-vault-resource-policy",
    name="concourse-vault",
    policy=Path(__file__).parent.joinpath("concourse_vault_policy.hcl").read_text(),
)
vault.aws.AuthBackendRole(
    "concourse-vault-resource-ec2-vault-auth",
    backend="aws",
    auth_type="iam",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[
        iam_instance_profile.arn
        for iam_instance_profile in concourse_worker_instance_profiles
    ],
    role="concourse-vault-resource",
    bound_ami_ids=[concourse_worker_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[ops_vpc_id],
    token_policies=[concourse_vault_resource_policy.name],
)

# Create Route53 DNS records for Concourse web nodes
five_minutes = 60 * 5
route53.Record(
    "concourse-web-dns-record",
    name=concourse_config.require("web_host_domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[ol_web_as_setup.load_balancer.dns_name],
    zone_id=mitodl_zone_id,
)
