"""Deploy the Digital Credentials Sign and Verify service to AWS Fargate.

This module sets up the services necessary to deploy a set of Docker containers to run
the sign and verify service needed for the Digital Credentials project.

    - Create a secret in AWS secrets manager

    - Create an IAM role and profile for the Fargate task

    - Create a cluster, task definition, and service on Amazon Fargate/ECS

    - Register the service with Route 53 DNS
"""

import base64
import json
import os
from pathlib import Path

from pulumi import Config, Output, StackReference
from pulumi_aws import acm, ecs, iam, lb, route53, secretsmanager

from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT
from bridge.secrets.sops import read_json_secrets
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
dns_stack = StackReference("infrastructure.aws.dns")
iam_policies = StackReference("infrastructure.aws.policies").require_output(
    "iam_policies"
)
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
apps_vpc = network_stack.require_output("applications_vpc")
aws_config = AWSBase(
    tags={
        "OU": "digital-credentials",
        "Environment": f"applications-{stack_info.env_suffix}",
    }
)

CONTAINER_PORT = 80

# Create an application load balancer to route traffic to the Fargate service

sign_and_verify_load_balancer = lb.LoadBalancer(
    f"sign-and-verify-load-balancer-{stack_info.env_suffix}",
    name=f"sign-and-verify-load-balancer-{stack_info.env_suffix}",
    ip_address_type="dualstack",
    load_balancer_type="application",
    enable_http2=True,
    subnets=apps_vpc["subnet_ids"],
    security_groups=[
        apps_vpc["security_groups"]["web"],
        apps_vpc["security_groups"]["default"],
    ],
    tags=aws_config.merged_tags(
        {"Name": f"sign-and-verify-load-balancer-{stack_info.env_suffix}"}
    ),
)

sign_and_verify_target_group = lb.TargetGroup(
    f"sign-and-verify-alb-target-group-{stack_info.env_suffix}",
    vpc_id=apps_vpc["id"],
    target_type="ip",
    port=CONTAINER_PORT,
    protocol="HTTP",
    health_check=lb.TargetGroupHealthCheckArgs(
        healthy_threshold=2,
        interval=10,
        path="/status",
        port=f"{CONTAINER_PORT}",
        protocol="HTTP",
    ),
    name=f"sign-and-verify-alb-group-{stack_info.env_suffix}",
    tags=aws_config.tags,
)

sign_and_verify_acm_cert = acm.get_certificate(
    domain="*.odl.mit.edu", most_recent=True, statuses=["ISSUED"]
)

sign_and_verify_alb_listener = lb.Listener(
    f"sign-and-verify-alb-listener-{stack_info.env_suffix}",
    certificate_arn=sign_and_verify_acm_cert.arn,
    load_balancer_arn=sign_and_verify_load_balancer.arn,
    port=DEFAULT_HTTPS_PORT,
    protocol="HTTPS",
    default_actions=[
        lb.ListenerDefaultActionArgs(
            type="forward",
            target_group_arn=sign_and_verify_target_group.arn,
        )
    ],
)

# Store the Unlocked DID in AWS secrets manager because it contains private key
# information

sign_and_verify_config = Config("sign_and_verify")
unlocked_did_secret = secretsmanager.Secret(
    f"sign-and-verify-unlocked-did-{stack_info.env_suffix}",
    description=(
        "Base64 encoded JSON object of the Unlocked DID that specifies the "
        "signing keys for the digital credentials sign and verify service."
    ),
    name_prefix=f"sign-and-verify-unlocked-did-{stack_info.env_suffix}",
    tags=aws_config.tags,
)

unlocked_did_secret_value = secretsmanager.SecretVersion(
    f"sign-and-verify-unlocked-did-value-{stack_info.env_suffix}",
    secret_id=unlocked_did_secret.id,
    secret_string=Output.secret(
        base64.b64encode(
            json.dumps(
                read_json_secrets(
                    Path(
                        f"digital_credentials/sign_and_verify.{stack_info.env_suffix}.json"
                    )
                )["secretKeySeed"]
            ).encode("utf8")
        ).decode("utf8")
    ),  # Base64 encoded JSON object of unlocked DID
)

hmac_secret = secretsmanager.Secret(
    f"sign-and-verify-hmac-{stack_info.env_suffix}",
    description="Shared secret for validating HMAC",
    name_prefix=f"sign-and-verify-hmac-secret-{stack_info.env_suffix}",
    tags=aws_config.tags,
)

hmac_secret_value = secretsmanager.SecretVersion(
    f"sign-and-verify-hmac-{stack_info.env_suffix}",
    secret_id=hmac_secret.id,
    secret_string=sign_and_verify_config.require_secret("hmac_secret"),
)
# Create the task execution role to grant access to retrieve the Unlocked DID secret and
# send logs to Cloudwatch

sign_and_verify_task_execution_role = iam.Role(
    "digital-credentials-sign-and-verify-task-execution-role",
    name=f"digital-credentials-sign-and-verify-execution-role-{stack_info.env_suffix}",
    path=f"/digital-credentials/sign-and-verify-execution-{stack_info.env_suffix}/",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ecs-tasks.amazonaws.com"},
            },
        }
    ),
    tags=aws_config.tags,
)

sign_and_verify_execution_policy = iam.Policy(
    "ecs-fargate-sign-and-verify-task-execution-policy",
    description=(
        "ECS Fargate task execution policy for sign and verify service to "
        "grant access for retrieving the Unlocked DID value from AWS Secrets Manager"
    ),
    name=f"ecs-fargate-sign-and-verify-task-execution-policy-{stack_info.env_suffix}",
    path=f"/digital-credentials/sign-and-verify-execution-{stack_info.env_suffix}/",
    policy=Output.all(unlocked_did_secret.arn, hmac_secret.arn).apply(
        lambda arns: lint_iam_policy(
            {
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": [
                            "secretsmanager:GetSecretValue",
                        ],
                        "Resource": arns,
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["kms:Decrypt"],
                        "Resource": ["arn:*:kms:*:*:key/secretsmanager"],
                    },
                ],
            },
            stringify=True,
        )
    ),
)

iam.RolePolicyAttachment(
    "sign-and-verify-task-execution-role-policy-attachment",
    policy_arn=sign_and_verify_execution_policy.arn,
    role=sign_and_verify_task_execution_role.name,
)

iam.RolePolicyAttachment(
    "sign-and-verify-task-execution-create-log-group-permissions",
    policy_arn=iam_policies["cloudwatch_logging"],
    role=sign_and_verify_task_execution_role.name,
)

# Create an ECS/Fargate cluster,define the task including container details, and
# register that with a service
sign_and_verify_cluster = ecs.Cluster(
    f"ecs-cluster-sign-and-verify-{stack_info.env_suffix}",
    capacity_providers=["FARGATE"],
    name=f"sign-and-verify-{stack_info.env_suffix}",
    tags=aws_config.merged_tags({"Name": f"sign-and-verify-{stack_info.env_suffix}"}),
)

container_label = (
    sign_and_verify_config.get("docker_label")
    or os.environ.get("SIGN_AND_VERIFY_CONTAINER_LABEL")
    or "latest"
)
sign_and_verify_task = ecs.TaskDefinition(
    f"sign-and-verify-task-{stack_info.env_suffix}",
    cpu="256",
    memory="512",
    network_mode="awsvpc",
    requires_compatibilities=["FARGATE"],
    tags=aws_config.merged_tags({"Name": f"sign-and-verify-{stack_info.env_suffix}"}),
    execution_role_arn=sign_and_verify_task_execution_role.arn,
    family=f"sign-and-verify-task-{stack_info.env_suffix}",
    container_definitions=Output.all(unlocked_did_secret.arn, hmac_secret.arn).apply(
        lambda arns: json.dumps(
            [
                {
                    "name": "sign-and-verify",
                    "image": f"mitodl/sign-and-verify:{container_label}",
                    "environment": [
                        {"name": "PORT", "value": f"{CONTAINER_PORT}"},
                        {"name": "DIGEST_CHECK", "value": "true"},
                        {
                            "name": "ISSUER_MEMBERSHIP_REGISTRY_URL",
                            "value": "https://digitalcredentials.github.io/issuer-registry/registry.json",
                        },
                        {
                            "name": "OIDC_ISSUER_URL",
                            "value": sign_and_verify_config.require("oidc_issuer_url"),
                        },
                    ],
                    "secrets": [
                        {"name": "DID_SEED", "valueFrom": arns[0]},
                        {"name": "HMAC_SECRET", "valueFrom": arns[1]},
                    ],
                    "portMappings": [
                        {"containerPort": CONTAINER_PORT, "protocol": "tcp"}
                    ],
                    "logConfiguration": {
                        "logDriver": "awslogs",
                        "options": {
                            "awslogs-group": f"digital-credentials-sign-and-verify-{stack_info.env_suffix}",  # noqa: E501
                            "awslogs-region": "us-east-1",
                            "awslogs-stream-prefix": (
                                f"sign-and-verify-{stack_info.env_suffix}"
                            ),
                            "awslogs-create-group": "true",
                        },
                    },
                }
            ]
        )
    ),
)

sign_and_verify_service = ecs.Service(
    f"sign-and-verify-service-{stack_info.env_suffix}",
    cluster=sign_and_verify_cluster.arn,
    desired_count=2,
    health_check_grace_period_seconds=30,
    platform_version="LATEST",
    launch_type="FARGATE",
    name=f"sign-and-verify-service-{stack_info.env_suffix}",
    network_configuration=ecs.ServiceNetworkConfigurationArgs(
        subnets=apps_vpc["subnet_ids"],
        security_groups=[
            apps_vpc["security_groups"]["web"],
            apps_vpc["security_groups"]["default"],
        ],
        assign_public_ip=True,
    ),
    propagate_tags="SERVICE",
    tags=aws_config.merged_tags(
        {"Name": f"sign-and-verify-service-{stack_info.env_suffix}"}
    ),
    task_definition=sign_and_verify_task.arn,
    force_new_deployment=True,
    deployment_controller=ecs.ServiceDeploymentControllerArgs(type="ECS"),
    load_balancers=[
        ecs.ServiceLoadBalancerArgs(
            container_port=CONTAINER_PORT,
            container_name="sign-and-verify",
            target_group_arn=sign_and_verify_target_group.arn,
        )
    ],
)

# Create a DNS record to point to the ALB for routing inbound traffic.
five_minutes = 60 * 5
sign_and_verify_domain = route53.Record(
    f"sign-and-verify-{stack_info.env_suffix}-dns-record",
    name=sign_and_verify_config.require("domain"),
    type="CNAME",
    ttl=five_minutes,
    records=[sign_and_verify_load_balancer.dns_name],
    zone_id=mitodl_zone_id,
)
