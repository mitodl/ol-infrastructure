from pulumi import Config, StackReference, get_stack

env_config = Config("environment")
stack = get_stack()
stack_name = stack.split(".")[-1]
namespace = stack.rsplit(".", 1)[0]
env_suffix = stack_name.lower()
env_prefix = namespace.rsplit(".", 1)[-1]
network_stack = StackReference(f"infrastructure.aws.network.{stack_name}")
policy_stack = StackReference("infrastructure.aws.policies")
destination_vpc = network_stack.require_output(env_config.require("vpc_reference"))
peer_vpcs = destination_vpc["peers"].apply(
    lambda peers: {peer: network_stack.require_output(peer) for peer in peers}
)
