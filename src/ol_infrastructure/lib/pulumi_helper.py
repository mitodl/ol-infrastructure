"""Helpers for working with Pulumi stack names and stack references."""

from dataclasses import dataclass
from typing import Any

from pulumi import StackReference, get_stack
from pulumi.runtime import sync_await


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


def require_stack_output_value(
    stack_reference: StackReference, output_name: str
) -> Any:
    """Return a concrete stack output value from a StackReference."""
    output_details = sync_await._sync_await(  # noqa: SLF001
        stack_reference.get_output_details(output_name)
    )
    if output_details.value is not None:
        return output_details.value
    if output_details.secret_value is not None:
        return output_details.secret_value

    msg = f"Missing required stack output: {output_name}"
    raise ValueError(msg)
