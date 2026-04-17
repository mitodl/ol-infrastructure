"""Helpers for working with Pulumi stack names and stack references."""

import os
from dataclasses import dataclass
from typing import Any

import pulumi
import pulumi.log
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


def merge_otel_resource_attributes(
    env_vars: dict[str, Any],
    k8s_labels: dict[str, str],
) -> None:
    """Append k8s label key/value pairs to OTEL_RESOURCE_ATTRIBUTES in-place.

    If OTEL_RESOURCE_ATTRIBUTES already exists in ``env_vars`` its existing value is
    preserved and the label attributes are appended with a comma separator.  When the
    key is absent it is created with only the label attributes.

    :param env_vars: Mutable dict of environment variables to update.
    :param k8s_labels: Kubernetes label key/value pairs to encode as OTEL attributes.
    """
    # No-op when there are no labels to merge; avoid clobbering or altering
    # OTEL_RESOURCE_ATTRIBUTES with an empty string or trailing comma.
    if not k8s_labels:
        return

    # Sort labels to ensure deterministic ordering and avoid spurious diffs.
    k8s_label_attrs = ",".join(f"{k}={v}" for k, v in sorted(k8s_labels.items()))
    base_otel = env_vars.get("OTEL_RESOURCE_ATTRIBUTES")
    if base_otel:
        env_vars["OTEL_RESOURCE_ATTRIBUTES"] = f"{base_otel},{k8s_label_attrs}"
    else:
        env_vars["OTEL_RESOURCE_ATTRIBUTES"] = k8s_label_attrs


def get_docker_image_tag(app_prefix: str) -> str:
    """Return the Docker image tag or digest value for an application.

    Reads ``{app_prefix}_DOCKER_TAG`` (a Git tag, e.g. ``v1.2.3``) or
    ``{app_prefix}_DOCKER_SHA`` (a Docker image digest, e.g.
    ``sha256:1d1ac890...``).  Exactly one of the two must be set; setting
    both is an error.

    :param app_prefix: Upper-case application prefix, e.g. ``"MIT_LEARN"``.
    :returns: The raw value of whichever variable is set.
    :raises OSError: If both variables are set, or if neither is set.
    """
    tag_var = f"{app_prefix}_DOCKER_TAG"
    sha_var = f"{app_prefix}_DOCKER_SHA"
    tag_value = os.environ.get(tag_var)
    sha_value = os.environ.get(sha_var)
    pulumi.log.info(
        f"get_docker_image_tag({app_prefix!r}): "
        f"{tag_var}={tag_value!r}, {sha_var}={sha_value!r}"
    )

    if tag_value and sha_value:
        msg = (
            f"Cannot set both {tag_var} and {sha_var}. "
            "Provide exactly one of a Git tag or a Docker image digest."
        )
        raise OSError(msg)
    if not tag_value and not sha_value:
        msg = f"Either {tag_var} or {sha_var} must be set."
        raise OSError(msg)
    return tag_value or sha_value  # type: ignore[return-value]


def _normalize_digest(value: str) -> str:
    """Ensure a digest value has the ``sha256:`` prefix."""
    return value if value.startswith("sha256:") else f"sha256:{value}"


def format_docker_image_ref(repository: str, app_prefix: str) -> str:
    """Return a fully-qualified Docker image reference string.

    When ``{app_prefix}_DOCKER_TAG`` is set the reference uses the tag
    separator (``:``)::

        mitodl/myapp:v1.2.3

    When ``{app_prefix}_DOCKER_SHA`` is set the reference uses the digest
    separator (``@``)::

        mitodl/myapp@sha256:1d1ac890...

    :param repository: Image repository path, e.g. ``"mitodl/myapp"``.
    :param app_prefix: Upper-case application prefix, e.g. ``"MIT_LEARN"``.
    :returns: Formatted image reference string.
    """
    sha_value = os.environ.get(f"{app_prefix}_DOCKER_SHA")
    tag_value = os.environ.get(f"{app_prefix}_DOCKER_TAG")
    # Validate exactly one is set (raises OSError on violation)
    get_docker_image_tag(app_prefix)

    if sha_value:
        return f"{repository}@{_normalize_digest(sha_value)}"
    return f"{repository}:{tag_value}"


def docker_image_config_kwargs(app_prefix: str) -> dict[str, str]:
    """Return the correct ``OLApplicationK8sConfig`` kwargs for the image ref.

    When ``{app_prefix}_DOCKER_TAG`` is set returns::

        {"application_docker_tag": "<tag>"}

    When ``{app_prefix}_DOCKER_SHA`` is set returns::

        {"application_image_digest": "sha256:<digest>"}

    Use by unpacking into ``OLApplicationK8sConfig``::

        OLApplicationK8sConfig(
            **docker_image_config_kwargs("MIT_LEARN"),
            ...
        )

    :param app_prefix: Upper-case application prefix, e.g. ``"MIT_LEARN"``.
    :returns: Dict with exactly one of ``application_docker_tag`` or
        ``application_image_digest``.
    """
    sha_value = os.environ.get(f"{app_prefix}_DOCKER_SHA")
    # Validate exactly one is set (raises OSError on violation)
    get_docker_image_tag(app_prefix)

    if sha_value:
        return {"application_image_digest": _normalize_digest(sha_value)}
    return {"application_docker_tag": os.environ[f"{app_prefix}_DOCKER_TAG"]}
