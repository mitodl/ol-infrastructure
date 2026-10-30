"""Mechanisms an application definition needs that do not depend on the application.

Nothing in this module may read Pulumi config, a stack reference, or a cloud
API, so that importing it needs no provider.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Literal

from kubernetes.utils.quantity import parse_quantity
from pulumi import Output
from pydantic import BaseModel, ConfigDict

from ol_infrastructure.components.services.apisix import (
    OLApisixOIDCSecretCredentials,
    OLApisixOIDCVaultCredentials,
)
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8sCeleryWorkerConfig,
)


class WorkloadResources(BaseModel):
    """Requests and limits for one workload, in the component's shape."""

    model_config = ConfigDict(extra="forbid")

    requests: dict[str, str]
    limits: dict[str, str]


class ClusterCapabilities(BaseModel):
    """What the target cluster provides beyond plain Kubernetes.

    The defaults describe EKS. A cluster without the AWS VPC CNI, ECR, KEDA, the
    VPA or the Prometheus operator turns the matching capability off.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, extra="forbid")

    registry: Literal["dockerhub", "ecr", "direct"] = "ecr"
    security_group_id: Output[str] | None = None
    security_group_name: Output[str] | None = None
    vault_auth_name: str | None = None
    autoscalers: bool = True
    pod_monitors: bool = True


def validate_queue_sizing(
    queues: dict[str, WorkloadResources], expected: Sequence[str]
) -> dict[str, WorkloadResources]:
    """Require ``queues`` to size every name in ``expected`` and nothing else."""
    expected_names = set(expected)
    if set(queues) != expected_names:
        msg = (
            f"celery_queues must size exactly {sorted(expected_names)}: "
            f"missing {sorted(expected_names - set(queues))}, "
            f"unknown {sorted(set(queues) - expected_names)}"
        )
        raise ValueError(msg)
    return queues


def celery_worker_configs(
    queue_names: Sequence[str],
    topology: Literal["per-queue", "merged"],
    queues: Mapping[str, WorkloadResources],
) -> list[OLApplicationK8sCeleryWorkerConfig]:
    """Return the celery workers for a topology, in ``queue_names`` order.

    A ``merged`` worker takes the requests and limits of the queue with the
    largest memory limit.
    """
    if topology == "merged":

        def memory_limit(resources: WorkloadResources) -> Decimal:
            limit = resources.limits.get("memory")
            return Decimal("Infinity") if limit is None else parse_quantity(limit)

        largest = max((queues[name] for name in queue_names), key=memory_limit)
        return [
            OLApplicationK8sCeleryWorkerConfig(
                # Explicit because the name lands in the Deployment name and a
                # label value, where the comma in queue_name is not valid.
                worker_name="all",
                queue_name=",".join(queue_names),
                resource_requests=largest.requests,
                resource_limits=largest.limits,
            )
        ]
    return [
        OLApplicationK8sCeleryWorkerConfig(
            queue_name=name,
            resource_requests=queues[name].requests,
            resource_limits=queues[name].limits,
        )
        for name in queue_names
    ]


def oidc_credentials(
    *, vault_auth_name: str | None, secret_name: str | None, vault_path: str
) -> OLApisixOIDCVaultCredentials | OLApisixOIDCSecretCredentials:
    """Return OIDC client credentials from Vault or from a Secret in the namespace.

    :raises ValueError: Unless exactly one of ``vault_auth_name`` and
        ``secret_name`` is set.
    """
    if secret_name is not None and vault_auth_name is None:
        return OLApisixOIDCSecretCredentials(secret_name=secret_name)
    if vault_auth_name is not None and secret_name is None:
        return OLApisixOIDCVaultCredentials(
            path=vault_path,
            vaultauth=vault_auth_name,
        )
    msg = (
        "The OIDC routes need exactly one credential source: "
        "cluster.vault_auth_name (from Vault) or oidc_secret_name "
        "(a Secret already in the namespace)."
    )
    raise ValueError(msg)
