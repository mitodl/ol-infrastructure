from pulumi import StackReference, get_stack

stack = get_stack()
stack_name = stack.split('.')[-1]
namespace = stack.rsplit('.', 1)[0]
env_suffix = stack_name.lower()
env_prefix = namespace.rsplit('.', 1)[-1]
network_stack = StackReference(f'infrastructure.aws.network.{stack_name}')
policy_stack = StackReference('infrastructure.aws.policies')
