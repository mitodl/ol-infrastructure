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
    """Return a concrete (eagerly-resolved) stack output value from a StackReference.

    This helper is intentionally synchronous and is only appropriate for
    program-bootstrapping code that must produce concrete Python values *before*
    any Pulumi resources are declared — for example, setting up a Kubernetes
    provider from a kubeconfig or driving conditional resource creation based on a
    storage-backend flag.  In those situations staying in the ``Output`` world via
    ``.apply()`` is not feasible.

    **Technical debt — private API:** The implementation calls
    ``pulumi.runtime.sync_await._sync_await``, an internal Pulumi function indicated
    by the leading underscore.  It is stable across current Pulumi 3.x releases but
    may break without a deprecation warning in a future version.  If breakage occurs,
    the migration path is to switch callers to the Pulumi Automation API's synchronous
    ``stack.outputs()`` method, or to restructure the program so that provider
    initialisation can be deferred into ``.apply()`` callbacks.

    **Secret outputs:** When *output_name* refers to a Pulumi secret, the raw
    plaintext value is returned as an ordinary Python object — Pulumi's
    secret-tracking is *not* preserved.  Only use this function with secret outputs
    when the value is consumed solely for bootstrapping (e.g. a kubeconfig passed
    directly to a provider constructor) and will *not* be re-exported, logged, or
    stored in a Pulumi resource input.
    """
    output_details = sync_await._sync_await(  # noqa: SLF001
        stack_reference.get_output_details(output_name)
    )
    if output_details.value is not None:
        return output_details.value
    if output_details.secret_value is not None:
        return output_details.secret_value

    msg = f"Missing required stack output: {output_name}"
    raise ValueError(msg)
