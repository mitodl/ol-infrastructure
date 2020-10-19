import json

from pulumi import Config, StackReference, get_stack
from pulumi_aws import ecs, iam, secretsmanager

from ol_infrastructure.lib.ol_types import AWSBase

stack = get_stack()
stack_name = stack.split('.')[-1]
namespace = stack.rsplit('.', 1)[0]
env_suffix = stack_name.lower()
network_stack = StackReference(f'infrastructure.aws.network.{stack_name}')
apps_vpc = network_stack.require_output('aplications_vpc')
aws_config = AWSBase(
    tags={
        'OU': 'digital-credentials',
        'Environment': 'applications-{env_suffix}'
    }
)

sign_and_verify_config = Config('sign_and_verify')
unlocked_did_secret = secretsmanager.Secret(
    f'sign-and-verify-unlocked-did-{env_suffix}',
    description='Base64 encoded JSON object of the Unlocked DID that specifies the signing keys '
    'for the digital credentials sign and verify service.',
    name=f'sign-and-verify-unlocked-did-{env_suffix}',
    tags=aws_config.tags
)

unlocked_did_secret_value = secretsmanager.SecretVersion(
    f'sign-and-verify-unlocked-did-value-{env_suffix}',
    secret_id=unlocked_did_secret.id,
    secret_string=sign_and_verify_config.require_secret('unlocked_did'),  # Base64 encoded JSON object of unlocked DID
)

sign_and_verify_task_execution_role = iam.Role(
    'digital-credentials-sign-and-verify-task-execution-role',
    name=f'digital-credentials-sign-and-verify-execution-role-{env_suffix}',
    path=f'/digital-credentials/sign-and-verify-execution-{env_suffix}/',
    assume_role_policy=json.dumps(
        {
            'Version': '2012-10-17',
            'Statement': {
                'Effect': 'Allow',
                'Action': 'sts:AssumeRole',
                'Principal': {'Service': 'ecs-tasks.amazonaws.com'}
            }
        }
    ),
    tags=aws_config.tags,
)

sign_and_verify_execution_policy = iam.Policy(
    'ecs-fargate-sign-and-verify-task-execution-policy',
    description='ECS Fargate task execution policy for sign and verify service to grant access for retrieving the '
    'Unlocked DID value from AWS Secrets Manager',
    name=f'ecs-fargate-sign-and-verify-task-execution-policy-{env_suffix}',
    path='/digital-credentials/sign-and-verify-execution-{env_suffix}/',
    policy=json.dumps(
        {
            'Version': '2012-10-17',
            'Statement': {
                'Effect': 'Allow',
                'Action': [
                    'secretsmanager:GetSecretValue',
                    'kms:Decrypt'
                ],
                'Resource': [
                    unlocked_did_secret.arn
                ]
            }
        }
    )
)

iam.RolePolicyAttachment(
    'sign-and-verify-task-execution-role-policy-attachment',
    policy_arn=sign_and_verify_execution_policy.arn,
    role=sign_and_verify_task_execution_role.name
)

sign_and_verify_cluster = ecs.Cluster(
    f'ecs-cluster-sign-and-verify-{env_suffix}',
    capacity_providers=['FARGATE'],
    name='sign-and-verify-{env_suffix}',
    tags=aws_config.merged_tags({'Name': 'sign-and-verify-{env_suffix}'}),
)

sign_and_verify_task = ecs.TaskDefinition(
    f'sign-and-verify-task-{env_suffix}',
    cpu='0.25',
    memory='500',
    network_mode='awsvpc',
    pid_mode='task',
    requires_compatibilities='FARGATE',
    tags=aws_config.tags,
    execution_role_arn=sign_and_verify_task_execution_role.arn,
    family=f'sign-and-verify-task-{env_suffix}',
    container_definitions=json.dumps(
        [
            {
                'name': 'sign-and-verify',
                'image': f'mitodl/sign-and-verify:{sign_and_verify_config.require("docker_label")}',
                'environment': [
                    {'name': 'PORT', 'value': '5000'}
                ],
                'secrets': [
                    {'name': 'UNLOCKED_DID', 'valueFrom': unlocked_did_secret.arn}
                ]
            },
            {
                'name': 'caddy',
                'image': 'mitodl/'
            }
        ]
    ),
    ipc_mode='task',
)

sign_and_verify_service = ecs.Service(
    f'sign-and-verify-service-{env_suffix}',
    cluster=sign_and_verify_cluster.arn,
    desired_count=1,
    launch_type='FARGATE',
    name=f'sign-and-verify-service-{env_suffix}',
    network_configuration=ecs.ServiceNetworkConfigurationArgs(
        subnets=apps_vpc['subnet_ids'],
        security_groups=apps_vpc['security_groups']['web'],
        assign_public_ip=True
    ),
    propagate_tags='SERVICE',
    tags=aws_config.merged_tags({'Name': f'sign-and-verify-service-{env_suffix}'}),
    task_definition=sign_and_verify_task.arn,
    force_new_deployment=True,
    deployment_controller=ecs.ServiceDeploymentControllerArgs(
        type='ECS'
    )
)
