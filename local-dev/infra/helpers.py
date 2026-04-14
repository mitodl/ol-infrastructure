"""Shared helpers for the local-dev Pulumi infra stack."""

import base64
from collections.abc import Callable
from pathlib import Path

import pulumi_kubernetes as k8s
from pulumi import ResourceOptions


def make_resource_opts(k8s_provider: k8s.Provider) -> Callable[..., ResourceOptions]:
    """Return a _k8s() factory bound to the given provider.

    Usage::

        _k8s = make_resource_opts(k8s_provider)
        resource = k8s.core.v1.Namespace("foo", opts=_k8s(parent=ns))
    """

    def _k8s(
        parent=None,
        depends_on=None,
        delete_before_replace: bool | None = None,  # noqa: FBT001
    ) -> ResourceOptions:
        return ResourceOptions(
            provider=k8s_provider,
            parent=parent,
            depends_on=depends_on or [],
            delete_before_replace=delete_before_replace,
        )

    return _k8s


def read_file_b64(path: Path) -> str:
    """Read *path* and return its contents as a base64-encoded string.

    Raises SystemExit with a human-readable message if the file is missing
    (e.g. setup.sh has not been run yet).
    """
    try:
        return base64.b64encode(path.read_bytes()).decode()
    except FileNotFoundError as e:
        msg = (
            f"Certificate file not found: {path}\n"
            "Run ./local-dev/scripts/setup.sh to generate local TLS certificates."
        )
        raise SystemExit(msg) from e
