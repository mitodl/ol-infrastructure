from dataclasses import dataclass

from pulumi import get_stack


@dataclass
class StackInfo:
    """Container class for enapsulating standard information about a stack."""

    name: str
    namespace: str
    env_suffix: str
    env_prefix: str
    full_name: str


def parse_stack() -> StackInfo:
    """Standardized method for extracting stack information.

    :returns: Parsed stack information for use in business logic.

    :rtype: StackInfo
    """
    stack = get_stack()
    stack_name = stack.split(".")[-1]
    namespace = stack.rsplit(".", 1)[0]
    return StackInfo(
        name=stack_name,
        namespace=namespace,
        env_suffix=stack_name.lower(),
        env_prefix=namespace.rsplit(".", 1)[-1],
        full_name=stack,
    )
