from dataclasses import dataclass
from typing import Text

from pulumi import get_stack


@dataclass
class StackInfo:
    """Container class for enapsulating standard information about a stack."""

    name: Text
    namespace: Text
    env_suffix: Text
    env_prefix: Text


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
    )
