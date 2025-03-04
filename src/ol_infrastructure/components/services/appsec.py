from pulumi_aws import ec2

from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
################################################
# Application security group
# Needs to happen ebfore the database security group is created
application_security_group = ec2.SecurityGroup(
    f"learn-ai-application-security-group-{stack_info.env_suffix}",
    name=f"learn-ai-application-security-group-{stack_info.env_suffix}",
    description="Access control for the learn-ai application pods.",
    # allow all egress traffic
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(
        k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs,
    ),
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

