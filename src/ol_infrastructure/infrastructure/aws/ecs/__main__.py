import base64
import json
import textwrap
from pathlib import Path
from typing import Any

import pulumi
import pulumi_vault as vault
import yaml
from bridge.lib.magic_numbers import (
    AWS_LOAD_BALANCER_NAME_MAX_LENGTH,
    AWS_TARGET_GROUP_NAME_MAX_LENGTH,
    DEFAULT_HTTP_PORT,
    DEFAULT_HTTPS_PORT,
)
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, ResourceOptions
from pulumi_aws import autoscaling, ec2, ecs, iam, lb

from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

if Config("vault_server").get("env_namespace"):
    setup_vault_provider()


def traefik_task_policy_document(cluster_arn) -> dict[str, Any]:
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "TraefikECSListClusters",
                "Effect": "Allow",
                "Action": [
                    "ecs:ListClusters",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "TraefikECSGetClusterInfo",
                "Effect": "Allow",
                "Action": [
                    "ecs:DescribeClusters",
                ],
                "Resource": [f"{cluster_arn}"],
            },
            {
                "Sid": "TraefikECSReadTasks",
                "Effect": "Allow",
                "Action": [
                    "ecs:DescribeTaskDefinition",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "TraefikECSReadClusterWorkload",
                "Effect": "Allow",
                "Action": [
                    "ecs:ListTasks",
                    "ecs:DescribeTasks",
                    "ecs:DescribeContainerInstances",
                ],
                "Resource": ["*"],
            },
            {
                "Sid": "TraefikECSReadServiceDiscoveryInfo",
                "Effect": "Allow",
                "Action": [
                    "servicediscovery:Get*",
                    "servicediscovery:List*",
                    "servicediscovery:DiscoverInstances",
                ],
                "Resource": ["*"],
            },
        ],
    }


###############
# STACK SETUP #
###############
cluster_config = pulumi.Config("cluster")
env_config = pulumi.Config("environment")
stack_info = parse_stack()

network_stack = pulumi.StackReference(f"infrastructure.aws.network.{stack_info.name}")
consul_stack = pulumi.StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)
vault_stack = pulumi.StackReference(
    f"infrastructure.vault.operations.{stack_info.name}"
)
policy_stack = pulumi.StackReference("infrastructure.aws.policies")

#############
# VARIABLES #
#############
environment_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc = network_stack.require_output(env_config.require("target_vpc"))
business_unit = env_config.get("business_unit") or "operations"
aws_config = AWSBase(tags={"OU": business_unit, "Environment": environment_name})

cluster_name = f"{environment_name}-ecs-cluster"
grafana_credentials = read_yaml_secrets(
    Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
)
consul_datacenter = consul_stack.require_output("datacenter")

vault_config = json.dumps(
    {
        "vault": {
            "address": "https://vault.query.consul:8200",
            "tls_skip_verify": True,
        },
        "auto_auth": {
            "method": {
                "type": "aws",
                "mount_path": "auth/aws",
                "config": {
                    "type": "iam",
                    "role": f"{environment_name}-traefik-task",
                    "region": "us-east-1",
                },
            },
            "sink": [
                {
                    "type": "file",
                    "derive_key": False,
                    "config": [{"path": "/root/token/.vault_token"}],
                }
            ],
        },
        "cache": {"use_auto_auth_token": "force"},
        "exit_after_auth": False,
        "listener": [{"tcp": {"address": "127.0.0.1:8200", "tls_disable": True}}],
    }
)

consul_template_config = json.dumps(
    {
        "consul": {
            "address": "127.0.0.1:8500",
        },
        "vault": {
            "address": "https://vault.query.consul:8200",
            "vault_agent_token_file": "/home/consul-template/token/.vault_token",
        },
        "templates": [
            {
                "destination": "/home/consul-template/tls/star.odl.mit.edu.key",
                "create_dest_dirs": False,
                "contents": '{{ with secret "secret-operations/global/odl_wildcard_cert" }}{{ printf .Data.value }}{{ end }}',
                "error_on_missing_key": True,
                "left_delimiter": "{{",
                "right_delimiter": "}}",
            },
            {
                "destination": "/home/consul-template/tsl/star.odl.mit.edu.crt",
                "create_dest_dirs": False,
                "contents": '{{ with secret "secret-odl-video-service/ovs-secrets" }}{{ printf .Data.data.nginx.tls_certificate }}{{ end }}',
                "error_on_missing_key": True,
                "left_delimiter": "{{",
                "right_delimiter": "}}",
            },
        ],
    }
)

user_data = consul_datacenter.apply(
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
                            "path": "/etc/default/vector",
                            "content": textwrap.dedent(
                                f"""\
                                            ENVIRONMENT={consul_dc}
                        APPLICATION=ovs
                        SERVICE=ovs
                        VECTOR_CONFIG_DIR=/etc/vector/
                        AWS_REGION={aws_config.region}
                        GRAFANA_CLOUD_API_KEY={grafana_credentials['api_key']}
                        GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials['prometheus_user_id']}
                        GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials['loki_user_id']}
                        """
                            ),
                            "owner": "root:root",
                        },
                        {
                            "path": "/etc/ecs/ecs.config",
                            "content": f"ECS_CLUSTER={cluster_name}",
                            "owner": "root:root",
                        },
                    ],
                }
            ),
        ).encode("utf8")
    ).decode("utf8")
)

##########
# CREATE #
##########


ecs_ami_id = "ami-0d8b4d379c115ad61"

launch_template = ec2.LaunchTemplate(
    f"{environment_name}-ecs-launch-template",
    image_id=ecs_ami_id,
    key_name="oldevops",
    iam_instance_profile=ec2.LaunchTemplateIamInstanceProfileArgs(
        arn="arn:aws:iam::610119931565:instance-profile/ecsInstanceRole",
    ),
    instance_type=cluster_config.get("instance_size") or "t3a.medium",
    user_data=user_data,
    vpc_security_group_ids=[
        target_vpc["security_groups"]["web"],
        consul_stack.require_output("security_groups")["consul_agent"],
    ],
)
autoscaling_group_tags = [
    autoscaling.GroupTagArgs(
        key=key_name,
        value=key_value,
        propagate_at_launch=True,
    )
    for key_name, key_value in aws_config.merged_tags(
        {"ami_id": ecs_ami_id, "AmazonECSManaged": "true"}
    ).items()
]

autoscaling_group = autoscaling.Group(
    f"{environment_name}-ecs-autoscalinggroup",
    max_size=cluster_config.get("max_size") or 3,
    min_size=cluster_config.get("min_size") or 2,
    desired_capacity=cluster_config.get("desired_size") or 2,
    vpc_zone_identifiers=target_vpc["subnet_ids"],
    launch_template=autoscaling.GroupLaunchTemplateArgs(
        id=launch_template.id,
        version="$Latest",
    ),
    instance_refresh=autoscaling.GroupInstanceRefreshArgs(
        strategy="Rolling",
        preferences=autoscaling.GroupInstanceRefreshPreferencesArgs(
            min_healthy_percentage=50,
        ),
        triggers=["tag"],
    ),
    tags=autoscaling_group_tags,
)

capacity_provider = ecs.CapacityProvider(
    f"{environment_name}-ecs-capacity-provider",
    auto_scaling_group_provider=ecs.CapacityProviderAutoScalingGroupProviderArgs(
        auto_scaling_group_arn=autoscaling_group.arn,
        managed_termination_protection="DISABLED",
        managed_scaling=ecs.CapacityProviderAutoScalingGroupProviderManagedScalingArgs(
            instance_warmup_period=300,
            maximum_scaling_step_size=1,
            minimum_scaling_step_size=1,
            status="DISABLED",
            target_capacity=10,
        ),
    ),
)

ecs_cluster = ecs.Cluster(
    cluster_name,
    name=cluster_name,
    tags=aws_config.merged_tags(),
    opts=ResourceOptions(depends_on=[capacity_provider]),
)

cluster_capacity_provider = ecs.ClusterCapacityProviders(
    f"{environment_name}-ecs-cluster-capacity-provider",
    capacity_providers=[capacity_provider.name],
    cluster_name=ecs_cluster.name,
    opts=ResourceOptions(depends_on=[capacity_provider, ecs_cluster]),
)

if cluster_config.get_bool("traefik_enabled") or True:
    # IAM configuration items
    traefik_assume_role_policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }

    traefik_execution_policy_document = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "logs:CreateLogGroup",
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                "Resource": ["*"],
            }
        ],
    }
    parliament_config = {  # type: Dict[str, Dict]
        "RESOURCE_STAR": {"ignore_locations": []},
    }

    # Execution role IAM items
    traefik_execution_policy = iam.Policy(
        f"{environment_name}-ecs-traefik-execution-policy",
        name=f"{environment_name}-ecs-traefik-execution-policy",
        path=f"/ol-operations/ecs/{environment_name}/",
        policy=lint_iam_policy(
            traefik_execution_policy_document,
            stringify=True,
            parliament_config=parliament_config,
        ),
        description="Role that the Amazon ECS container agent and the Docker"
        " daemon can assumm in {cluster_name}-ecs-cluster.",
    )
    traefik_execution_role = iam.Role(
        f"{environment_name}-ecs-traefik-execution-role",
        name=f"{environment_name}-ecs-traefik-execution-role",
        assume_role_policy=traefik_assume_role_policy_document,
        tags=aws_config.tags,
    )
    iam.RolePolicyAttachment(
        f"{environment_name}-ecs-traefik-execution-role-policy-attachment",
        policy_arn=traefik_execution_policy.arn,
        role=traefik_execution_role.name,
    )

    # Task role IAM items
    traefik_task_policy = iam.Policy(
        f"{environment_name}-ecs-traefik-task-policy",
        name=f"{environment_name}-ecs-traefik-task-policy",
        path=f"/ol-operations/ecs/{environment_name}/",
        policy=ecs_cluster.arn.apply(
            lambda arn: lint_iam_policy(
                traefik_task_policy_document(arn),
                stringify=True,
                parliament_config=parliament_config,
            )
        ),
        description=f"Allows the traefik tasks in {cluster_name}-ecs-cluster"
        " to discover tasks on ECS clusters.",
    )
    traefik_task_role = iam.Role(
        f"{environment_name}-ecs-traefik-task-role",
        name=f"{environment_name}-ecs-traefik-task-role",
        assume_role_policy=traefik_assume_role_policy_document,
        tags=aws_config.tags,
    )
    iam.RolePolicyAttachment(
        f"{environment_name}-ecs-traefik-task-role-policy-attachment",
        policy_arn=traefik_task_policy.arn,
        role=traefik_task_role.name,
    )
    iam.RolePolicyAttachment(
        f"{environment_name}-ecs-traefik-task-role-describeinstances-policy-attachment",
        policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
        role=traefik_task_role.name,
    )

    traefik_task_vault_policy = vault.Policy(
        f"{environment_name}-traefik-task-vault-policy",
        name=f"{environment_name}-traefik-task",
        policy=Path(__file__).parent.joinpath("traefik_policy.hcl").read_text(),
    )

    vault.aws.AuthBackendRole(
        f"{environment_name}-traefik-task-vault-auth",
        backend="aws",
        auth_type="iam",
        role=f"{environment_name}-traefik-task",
        bound_iam_principal_arns=[traefik_task_role.arn.apply(lambda arn: f"{arn}")],
        token_policies=[traefik_task_vault_policy.name],
    )

    refresh_interval = cluster_config.get_int("traefik_refresh_interval_seconds") or 15
    log_level = cluster_config.get("traefik_log_level") or "WARN"
    traefik_container_name = f"{environment_name}-traefik"
    traefik_task_definition = ecs.TaskDefinition(
        f"{environment_name}-traefik-ingress-task-definition",
        family=f"{environment_name}-traefik",
        execution_role_arn=traefik_execution_role.arn.apply(lambda arn: f"{arn}"),
        task_role_arn=traefik_task_role.arn.apply(lambda arn: f"{arn}"),
        container_definitions=json.dumps(
            [
                {
                    "name": traefik_container_name,
                    "image": "traefik:v2.10.4",
                    "essential": True,
                    "mountPoints": [
                        {
                            "sourceVolume": "wildcard-certificate",
                            "containerPath": "/etc/traefik/tls",
                            "readOnly": True,
                        },
                    ],
                    "cpu": 512,
                    "memory": 512,
                    #                    "dependsOn": [
                    #                    ],
                    "command": [
                        "--api.insecure=true",
                        f"--entryPoints.http.address=:{DEFAULT_HTTP_PORT}",
                        f"--entryPoints.https.address=:{DEFAULT_HTTPS_PORT}",
                        "--entryPoints.http.http.redirections.entryPoint.to=https",
                        "--entryPoints.http.http.redirections.entryPoint.scheme=https",
                        "--providers.ecs=true",
                        "--providers.ecs.exposedByDefault=false",
                        "--providers.ecs.autoDiscoverClusters=true",
                        "--providers.ecs.clusters={cluster_name}",
                        f"--providers.ecs.refreshSeconds={refresh_interval}",
                        f"--log.level={log_level}",
                        "--accesslog=true",
                    ],
                    "portMappings": [
                        {
                            "name": f"{cluster_name}-traefik-{DEFAULT_HTTP_PORT}-tcp",
                            "containerPort": DEFAULT_HTTP_PORT,
                            "hostPort": DEFAULT_HTTP_PORT,
                            "protocol": "tcp",
                        },
                        {
                            "name": f"{cluster_name}-traefik-{DEFAULT_HTTPS_PORT}-tcp",
                            "containerPort": DEFAULT_HTTPS_PORT,
                            "hostPort": DEFAULT_HTTPS_PORT,
                            "protocol": "tcp",
                        },
                    ],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-create-group": "true",
                            "awslogs-group": f"/ecs/{cluster_name}/traefik",
                            "awslogs-region": "us-east-1",
                            "awslogs-stream-prefix": "ecs",
                        },
                    },
                },
                {
                    "name": "traefik-vault-agent",
                    "image": "hashicorp/vault:latest",
                    "essential": False,
                    "mountPoints": [
                        {
                            "sourceVolume": "vault-token",
                            "containerPath": "/root/token",
                            "readOnly": False,
                        }
                    ],
                    "environment": [
                        {"name": "VAULT_SKIP_VERIFY", "value": "true"},
                        {
                            "name": "VAULT_CONFIG",
                            "value": base64.b64encode(
                                vault_config.encode("utf8")
                            ).decode("utf8"),
                        },
                    ],
                    "entrypoint": ["/bin/sh"],
                    "command": [
                        "echo $VAULT_CONFIG | base64 -d > /root/vault.json && vault agent -config=/root/vault.json",
                    ],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-create-group": "true",
                            "awslogs-group": f"/ecs/{cluster_name}/traefik",
                            "awslogs-region": "us-east-1",
                            "awslogs-stream-prefix": "ecs",
                        },
                    },
                    "cpu": 128,
                    "memory": 128,
                    "healthCheck": {
                        "command": [
                            "CMD-SHELL",
                            "vault agent --help",
                        ],
                        "interval": 5,
                        "timeout": 2,
                        "retries": 3,
                    },
                },
            ]
        ),
        cpu="1024",
        memory="1024",
        network_mode="host",
        placement_constraints=[],
        requires_compatibilities=["EC2"],
        volumes=[
            ecs.TaskDefinitionVolumeArgs(
                name="vault-token",
                docker_volume_configuration=ecs.TaskDefinitionVolumeDockerVolumeConfigurationArgs(
                    scope="task",
                    driver="local",
                ),
            ),
            ecs.TaskDefinitionVolumeArgs(
                name="wildcard-certificate",
                docker_volume_configuration=ecs.TaskDefinitionVolumeDockerVolumeConfigurationArgs(
                    scope="task",
                    driver="local",
                ),
            ),
        ],
        opts=ResourceOptions(depends_on=[traefik_execution_role, traefik_task_role]),
    )

    load_balancer = lb.LoadBalancer(
        f"{environment_name}-ecs-lb-"[:AWS_LOAD_BALANCER_NAME_MAX_LENGTH].rstrip("-"),
        load_balancer_type="application",
        subnets=target_vpc["subnet_ids"],
        security_groups=[target_vpc["security_groups"]["web"]],
        enable_deletion_protection=False,
        enable_http2=True,
        tags=aws_config.tags,
    )

    http_target_group = lb.TargetGroup(
        f"{environment_name}-ecs-http-tg-"[:AWS_TARGET_GROUP_NAME_MAX_LENGTH].rstrip(
            "-"
        ),
        target_type="instance",
        # ),
        port=DEFAULT_HTTP_PORT,
        protocol="HTTP",
        vpc_id=target_vpc["id"],
    )
    https_target_group = lb.TargetGroup(
        f"{environment_name}-ecs-https-tg-"[:AWS_TARGET_GROUP_NAME_MAX_LENGTH].rstrip(
            "-"
        ),
        target_type="instance",
        health_check=lb.TargetGroupHealthCheckArgs(
            healthy_threshold=2,
            interval=10,
            path="/",
            port=str(DEFAULT_HTTPS_PORT),
            protocol="HTTPS",
            matcher="404",
        ),
        port=DEFAULT_HTTPS_PORT,
        protocol="HTTPS",
        vpc_id=target_vpc["id"],
    )

    http_listener = lb.Listener(
        f"{environment_name}-ecs-http-listener",
        load_balancer_arn=load_balancer.arn,
        port=DEFAULT_HTTP_PORT,
        protocol="HTTP",
        default_actions=[
            lb.ListenerDefaultActionArgs(
                type="forward",
                target_group_arn=http_target_group.arn,
            )
        ],
    )

    https_listener = lb.Listener(
        f"{environment_name}-ecs-https-listener",
        load_balancer_arn=load_balancer.arn,
        port=DEFAULT_HTTPS_PORT,
        protocol="HTTP",
        default_actions=[
            lb.ListenerDefaultActionArgs(
                type="forward",
                target_group_arn=https_target_group.arn,
            ),
        ],
    )

    traefik_service = ecs.Service(
        f"{environment_name}-traefik-service",
        name=f"{environment_name}-traefik",
        cluster=ecs_cluster.id,
        #    ecs.ServiceCapacityProviderStrategyArgs(
        # ],
        # ),
        health_check_grace_period_seconds=120,
        scheduling_strategy="DAEMON",
        task_definition=traefik_task_definition.arn,
        load_balancers=[
            ecs.ServiceLoadBalancerArgs(
                target_group_arn=http_target_group.arn,
                container_name=traefik_container_name,
                container_port=DEFAULT_HTTP_PORT,
            ),
            ecs.ServiceLoadBalancerArgs(
                target_group_arn=https_target_group.arn,
                container_name=traefik_container_name,
                container_port=DEFAULT_HTTPS_PORT,
            ),
        ],
        tags=aws_config.tags,
        opts=ResourceOptions(depends_on=[traefik_task_definition]),
    )
#
#
#
#
## Consul Service
#    "consul-provider",
#        read_yaml_secrets(Path(f"pulumi/consul.{stack_info.env_suffix}.yaml"))[
#            "basic_auth_password"
#    ),
#    "aws-opensearch-consul-node",
#    "aws-opensearch-consul-service",
#        "external-node": True,
#        "external-probe": True,
#    },
#        consul.ServiceCheckArgs(
#            ).apply(lambda es: "{address}:{port}".format(**es)),
#    ],
#
# consul.Keys(
#    "elasticsearch",
#        consul.KeysKeyArgs(
#        ),
#    ],
#
#
## Export Resources for shared use
# pulumi.export(
#    "cluster",
#    },
