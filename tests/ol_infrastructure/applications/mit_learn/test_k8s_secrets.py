"""Pin the VSO resources mit_learn's deployed program creates.

``vso_secrets_main.json`` was generated once by ``capture_stacks`` from
k8s_secrets.py as it stood on main at 35dc2e385, before the variables were
described as data. It proves the description reproduces that file for each
stack's shape; a difference here is a change to what deployed stacks get, so
regenerate the fixture only on purpose.
"""

import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from ol_infrastructure.components.services.vault import OLVaultRestartTarget
from ol_infrastructure.lib.pulumi_helper import StackInfo
from tests.ol_infrastructure.applications.vso_capture import (
    MountSpec,
    capture_vso_secrets,
)

FIXTURE = Path(__file__).parent / "fixtures" / "vso_secrets_main.json"
DEPLOYMENTS = ["mitlearn-app", "mitlearn-celery-beat"]
# Each stack's shape: whether it reads StarRocks (STARROCKS_HOST set) and runs
# the memory service (mitlearn:enable_memory).
STACKS = {
    "CI": {"starrocks": False, "memory": False},
    "QA": {"starrocks": True, "memory": True},
    "Production": {"starrocks": True, "memory": False},
}
MOUNTS = {
    "db": MountSpec("postgres-mitlearn", "database", "<database mount>"),
    "mitlearn": MountSpec("secret-mitlearn", "kv-v2", "<mitlearn mount>"),
}


def capture(module: ModuleType, stack: str) -> dict[str, Any]:
    """Call ``module.create_mitlearn_k8s_secrets`` with ``stack``-shaped inputs."""
    shape = STACKS[stack]
    env_suffix = stack.lower()
    return capture_vso_secrets(
        project="ol-application-mitlearn",
        stack=stack,
        mounts=MOUNTS,
        create=lambda mounts, cache: module.create_mitlearn_k8s_secrets(
            stack_info=StackInfo(
                name=stack,
                namespace="applications.mit_learn",
                env_suffix=env_suffix,
                env_prefix="applications",
                full_name=f"applications.mit_learn.{stack}",
                k8s_name=env_suffix,
            ),
            mitlearn_namespace="mitlearn",
            k8s_global_labels={"ol.mit.edu/application": "mitlearn"},
            vault_k8s_resources=SimpleNamespace(auth_name="mitlearn"),
            mitlearn_vault_mount=mounts["mitlearn"],
            db_config=SimpleNamespace(db_mount=mounts["db"]),
            redis_password="redis-password",  # pragma: allowlist secret
            redis_cache=cache,
            restart_targets=[
                OLVaultRestartTarget(kind="Deployment", name=name)
                for name in DEPLOYMENTS
            ],
            starrocks_vault_mount_path="database-starrocks"
            if shape["starrocks"]
            else None,
            memory_enabled=shape["memory"],
        ),
    )


def capture_stacks(module: ModuleType) -> dict[str, Any]:
    return {stack: capture(module, stack) for stack in STACKS}


@pytest.mark.parametrize("stack", STACKS)
def test_the_vso_resources_match_main_before_the_description(stack):
    # Imported here, not at module level: k8s_secrets pulls in an ElastiCache
    # helper that builds a boto3 client on import, and conftest only sets
    # AWS_REGION once collection is over.
    from ol_infrastructure.applications.mit_learn import (  # noqa: PLC0415
        k8s_secrets,
    )

    assert capture(k8s_secrets, stack) == json.loads(FIXTURE.read_text())[stack]
