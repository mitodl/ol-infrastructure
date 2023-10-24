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
from bridge.lib.magic_numbers import (
    CONCOURSE_WORKER_HEALTHCHECK_PORT,
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    MAXIMUM_PORT_NUMBER,
)
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, Output, StackReference
from pulumi_aws import acm, autoscaling, ec2, get_caller_identity, iam, lb, route53
from pulumi_consul import Node, Service, ServiceCheckArgs

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
                            "provider=aws tag_key=consul_env "
                            f"tag_value={consul_dc}"  # noqa: ISC001, RUF100
                        ],
                        "datacenter": consul_dc,
                    }
                ),
                "owner": "consul:consul",
            },
            {
                "path": "/etc/default/vector",
                "content": textwrap.dedent(f"""\
                    ENVIRONMENT={consul_dc}
                    APPLICATION=concourse-worker
                    SERVICE=concourse
                    VECTOR_CONFIG_DIR=/etc/vector/
                    AWS_REGION={aws_config.region}
                    GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                    GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                    GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                    """),
                "owner": "root:root",
            },
        ]
    }
    if concourse_team:
        yaml_contents["write_files"].append(
            {
                "path": "/etc/default/concourse-team",
                "content": textwrap.dedent(f"""\
                     CONCOURSE_TEAM={concourse_team}
                     """),
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

# Create worker node security group
concourse_worker_security_group = ec2.SecurityGroup(
    f"concourse-worker-security-group-{stack_info.env_suffix}",
    name=f"concourse-worker-operations-{stack_info.env_suffix}",
    description="Access control for Concourse worker servers",
    ingress=[],
    egress=default_egress_args,
    vpc_id=ops_vpc_id,
)

# Create web node security group
concourse_web_security_group = ec2.SecurityGroup(
    f"concourse-web-security-group-{stack_info.env_suffix}",
    name=f"concourse-web-operations-{stack_info.env_suffix}",
    description="Access control for Concourse web servers",
    ingress=[],
    egress=default_egress_args,
    vpc_id=ops_vpc_id,
)

# Link the two groups web    -> worker (all)
#                     worker -> worker (all)
#                     worker -> web    (all)
#         not needed: web    -> web    (all)
ec2.SecurityGroupRule(
    "concourse-worker-from-web-nodes",
    type="ingress",
    security_group_id=concourse_worker_security_group.id,
    source_security_group_id=concourse_web_security_group.id,
    protocol="tcp",
    from_port=0,
    to_port=MAXIMUM_PORT_NUMBER,
    description="Allow all traffic from Concourse web nodes to workers",
)
ec2.SecurityGroupRule(
    "concourse-worker-from-concourse-worker",
    type="ingress",
    security_group_id=concourse_worker_security_group.id,
    self=True,
    protocol="tcp",
    from_port=0,
    to_port=MAXIMUM_PORT_NUMBER,
    description="Allow all traffic from concourse workers to all other workers.",
)
ec2.SecurityGroupRule(
    "concourse-web-from-concourse-worker",
    type="ingress",
    security_group_id=concourse_web_security_group.id,
    source_security_group_id=concourse_worker_security_group.id,
    protocol="tcp",
    from_port=0,
    to_port=MAXIMUM_PORT_NUMBER,
    description="Allow all traffic from concourse workers to web nodes.",
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
concourse_db_config = OLPostgresDBConfig(
    instance_name=f"concourse-db-{stack_info.env_suffix}",
    password=concourse_config.require("db_password"),
    storage=concourse_config.get("db_capacity"),
    subnet_group_name=target_vpc["rds_subnet"],
    security_groups=[concourse_db_security_group],
    tags=aws_config.tags,
    db_name="concourse",
    engine_version="12.14",
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
web_lb = lb.LoadBalancer(
    "concourse-web-load-balancer",
    name=concourse_web_tag,
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=target_vpc["subnet_ids"],
    security_groups=[
        target_vpc["security_groups"]["web"],
    ],
    tags=aws_config.merged_tags({"Name": concourse_web_tag}),
)

TARGET_GROUP_NAME_MAX_LENGTH = 32
web_lb_target_group = lb.TargetGroup(
    "concourse-web-alb-target-group",
    vpc_id=ops_vpc_id,
    target_type="instance",
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=2,
        interval=10,
        path="/api/v1/info",
        port=str(DEFAULT_HTTPS_PORT),
        protocol="HTTPS",
    ),
    name=concourse_web_tag[:TARGET_GROUP_NAME_MAX_LENGTH],
    tags=aws_config.tags,
)
concourse_web_acm_cert = acm.get_certificate(
    domain="*.odl.mit.edu", most_recent=True, statuses=["ISSUED"]
)
concourse_web_alb_listener = lb.Listener(
    "concourse-web-alb-listener",
    certificate_arn=concourse_web_acm_cert.arn,
    load_balancer_arn=web_lb.arn,
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=web_lb_target_group.arn,
        )
    ],
)

# Create auto scale group and launch configs for Concourse web and worker
web_instance_type = (
    concourse_config.get("web_instance_type") or InstanceTypes.burstable_medium.name
)
consul_datacenter = consul_stack.require_output("datacenter")


web_launch_config = ec2.LaunchTemplate(
    "concourse-web-launch-template",
    name_prefix=f"concourse-web-{stack_info.env_suffix}-",
    description="Launch template for deploying Concourse web nodes",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn=concourse_instance_profile.arn,
    ),
    image_id=concourse_web_ami.id,
    block_device_mappings=[
        ec2.LaunchTemplateBlockDeviceMappingArgs(
            device_name="/dev/xvda",
            ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                volume_size=concourse_config.get_int("web_disk_size") or 25,
                volume_type=DiskTypes.ssd,
                delete_on_termination=True,
            ),
        )
    ],
    vpc_security_group_ids=[
        concourse_web_security_group.id,
        target_vpc["security_groups"]["web"],
        consul_security_groups["consul_agent"],
    ],
    instance_type=InstanceTypes[web_instance_type].value,
    key_name="oldevops",
    tag_specifications=[
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": concourse_web_tag}),
        ),
        ec2.LaunchTemplateTagSpecificationArgs(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": concourse_web_tag}),
        ),
    ],
    tags=aws_config.tags,
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
                                "content": textwrap.dedent(f"""\
                                    ENVIRONMENT={consul_dc}
                                    APPLICATION=concourse-web
                                    SERVICE=concourse
                                    VECTOR_CONFIG_DIR=/etc/vector/
                                    AWS_REGION={aws_config.region}
                                    GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                                    GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                                    GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                                    """),
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
web_asg = autoscaling.Group(
    "concourse-web-autoscaling-group",
    desired_capacity=web_asg_config["desired"] or 1,
    min_size=web_asg_config["min"] or 1,
    max_size=web_asg_config["max"] or 5,
    health_check_type="ELB",
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=web_launch_config.id, version="$Latest"
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50
        ),
        triggers=["tag"],
    ),
    target_group_arns=[web_lb_target_group.arn],
    tags=[
        autoscaling.GroupTagArgs(
            key=key_name,
            value=key_value,
            propagate_at_launch=True,
        )
        for key_name, key_value in aws_config.merged_tags(
            {"ami_id": concourse_web_ami.id, "concourse_type": "web"}
        ).items()
    ],
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
    worker_launch_config = ec2.LaunchTemplate(
        f"concourse-worker-{worker_class_name}-launch-template",
        name_prefix=f"concourse-worker-{worker_class_name}-{stack_info.env_suffix}-",
        description=(
            f"Launch template for deploying concourse worker-{worker_class_name} nodes."
        ),
        iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
            arn=concourse_worker_instance_profile.arn,
        ),
        image_id=concourse_worker_ami.id,
        block_device_mappings=[
            ec2.LaunchTemplateBlockDeviceMappingArgs(
                device_name="/dev/xvda",
                ebs=ec2.LaunchTemplateBlockDeviceMappingEbsArgs(
                    iops=worker_def.get("disk_iops", 3000),
                    throughput=worker_def.get("disk_throughput", 125),
                    volume_size=worker_def.get("disk_size_gb", 250),
                    volume_type=DiskTypes.ssd,
                    delete_on_termination=True,
                ),
            )
        ],
        vpc_security_group_ids=[
            concourse_worker_security_group.id,
            consul_security_groups["consul_agent"],
        ],
        instance_type=worker_instance_type,
        key_name="oldevops",
        metadata_options=ec2.LaunchTemplateMetadataOptionsArgs(
            http_endpoint="enabled",
            http_tokens="optional",
            http_put_response_hop_limit=5,
            instance_metadata_tags="enabled",
        ),
        tag_specifications=[
            ec2.LaunchTemplateTagSpecificationArgs(
                resource_type="instance",
                tags=aws_config.merged_tags(
                    {
                        "Name": f"concourse-worker-{worker_class_name}-{stack_info.env_suffix}"  # noqa: E501
                    },
                    worker_def["aws_tags"],
                ),
            ),
            ec2.LaunchTemplateTagSpecificationArgs(
                resource_type="volume",
                tags=aws_config.merged_tags(
                    {
                        "Name": f"concourse-worker-{worker_class_name}-{stack_info.env_suffix}"  # noqa: E501
                    },
                    worker_def["aws_tags"],
                ),
            ),
        ],
        tags=aws_config.merged_tags(worker_def["aws_tags"]),
        user_data=consul_datacenter.apply(build_worker_user_data_partial),
    )

    # We will create a 'fake' lb, targetgroup and lblistener to make use of
    # aws's ability to do healthchecks and automatically swap out stalled
    # concourse workers.
    # We will never actually interact with workers via these resources.
    worker_lb = lb.LoadBalancer(
        f"concourse-worker-{worker_class_name}-load-balancer",
        internal=True,
        ip_address_type="dualstack",
        load_balancer_type="application",
        name=(
            f"concourse-worker-alb-{worker_class_name[:3]}-{stack_info.env_suffix[:2]}"
        ),
        security_groups=[concourse_worker_security_group.id],
        subnets=target_vpc["subnet_ids"],
        tags=aws_config.merged_tags({}),
    )

    worker_target_group = lb.TargetGroup(
        f"concourse-worker-{worker_class_name}-target-group",
        vpc_id=ops_vpc_id,
        port=CONCOURSE_WORKER_HEALTHCHECK_PORT,
        protocol="HTTP",
        health_check=lb.TargetGroupHealthCheckArgs(
            healthy_threshold=2,
            interval=60,
            matcher="200",
            path="/",
            port=CONCOURSE_WORKER_HEALTHCHECK_PORT,
            protocol="HTTP",
            timeout=20,
            unhealthy_threshold=5,
        ),
        name=f"concourse-worker-tg-{worker_class_name[:3]}-{stack_info.env_suffix[:2]}"[
            :TARGET_GROUP_NAME_MAX_LENGTH
        ],
        tags=aws_config.merged_tags(worker_def["aws_tags"]),
    )

    worker_lb_alb_listener = lb.Listener(
        f"concourse-worker-{worker_class_name}-alb-listener",
        load_balancer_arn=worker_lb.arn,
        port=CONCOURSE_WORKER_HEALTHCHECK_PORT,
        protocol="HTTP",
        default_actions=[
            lb.ListenerDefaultActionArgs(
                type="forward",
                target_group_arn=worker_target_group.arn,
            )
        ],
    )

    auto_scale_config = worker_def["auto_scale"]
    worker_asg = autoscaling.Group(
        f"concourse-worker-{worker_class_name}-autoscaling-group",
        desired_capacity=auto_scale_config["desired"] or 1,
        min_size=auto_scale_config["min"] or 1,
        max_size=auto_scale_config["max"] or 50,
        health_check_type="ELB",
        vpc_zone_identifiers=target_vpc["subnet_ids"],
        launch_template=autoscaling.GroupLaunchTemplateArgs(
            id=worker_launch_config.id, version="$Latest"
        ),
        instance_refresh=autoscaling.GroupInstanceRefreshArgs(
            strategy="Rolling",
            preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
                min_healthy_percentage=50,
            ),
            triggers=["tag"],
        ),
        tags=[
            autoscaling.GroupTagArgs(
                key=key_name,
                value=key_value,
                propagate_at_launch=True,
            )
            for key_name, key_value in aws_config.merged_tags(
                {
                    "ami_id": concourse_worker_ami.id,
                    "concourse_type": f"worker-{worker_class_name}",
                },
            ).items()
        ],
        target_group_arns=[worker_target_group.arn],
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
    records=[web_lb.dns_name],
    zone_id=mitodl_zone_id,
)
