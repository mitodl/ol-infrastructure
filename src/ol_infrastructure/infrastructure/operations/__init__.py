from pulumi import Config, StackReference

from ol_infrastructure.lib.pulumi_helper import parse_stack

env_config = Config("environment")
stack_info = parse_stack()
stack_name = stack_info.name
env_suffix = stack_info.env_suffix
env_prefix = stack_info.env_prefix
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
destination_vpc = network_stack.require_output(env_config.require("vpc_reference"))
peer_vpcs = destination_vpc["peers"].apply(
    lambda peers: {peer: network_stack.require_output(peer) for peer in peers}
)
