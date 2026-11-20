"""Pin the VSO resources mit_learn's deployed program creates.

``vso_secrets_main.json`` was generated once by ``capture_stacks`` from
k8s_secrets.py as it stood on main at 35dc2e385, before the variables were
described as data. It proves the description reproduces that file for each
stack's shape; a difference here is a change to what deployed stacks get, so
regenerate the fixture only on purpose.
"""

import asyncio
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pulumi
import pulumi_vault as vault
import pytest

from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

FIXTURE = Path(__file__).parent / "fixtures" / "vso_secrets_main.json"
DEPLOYMENTS = ["mitlearn-app", "mitlearn-celery-beat"]
# Each stack's shape: whether it reads StarRocks (STARROCKS_HOST set) and runs
# the memory service (mitlearn:enable_memory).
STACKS = {
    "CI": {"starrocks": False, "memory": False},
    "QA": {"starrocks": True, "memory": True},
    "Production": {"starrocks": True, "memory": False},
}


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def capture(module: ModuleType, stack: str) -> dict[str, Any]:
    """Call ``module.create_mitlearn_k8s_secrets`` with ``stack``-shaped inputs.

    :returns: Each OLVaultK8SSecret's Pulumi name and config (templates as
        ordered pairs; the two Output mounts as markers), the Secret names in
        the order pods list them under ``envFrom``, the names pods mount as
        optional, and the type of each returned resource.
    """
    shape = STACKS[stack]
    env_suffix = stack.lower()
    pulumi.runtime.set_mocks(
        _Mocks(), project="ol-application-mitlearn", stack=stack, preview=False
    )
    configs: list[dict[str, Any]] = []
    state: dict[str, Any] = {}
    original_init = OLVaultK8SSecret.__init__

    def recording_init(self, name, resource_config, opts=None):
        mount = resource_config.mount
        markers = {
            id(state["db_path"]): "<database mount>",
            id(state["mitlearn_path"]): "<mitlearn mount>",
        }
        configs.append(
            {
                "resource_name": name,
                "config_class": type(resource_config).__name__,
                "mount": markers.get(id(mount), mount),
                "templates": [list(item) for item in resource_config.templates.items()],
                **resource_config.model_dump(
                    mode="json", exclude={"mount", "templates"}
                ),
            }
        )
        original_init(self, name, resource_config, opts)

    result: dict[str, Any] = {}

    @pulumi.runtime.test
    def run():
        db_mount = vault.Mount("db", path="postgres-mitlearn", type="database")
        mitlearn_mount = vault.Mount("mitlearn", path="secret-mitlearn", type="kv-v2")
        state["db_path"] = db_mount.path
        state["mitlearn_path"] = mitlearn_mount.path
        cache = vault.Mount("cache", path="cache", type="kv")
        # The redis Secret reads only .address; any Resource satisfies depends_on.
        setattr(cache, "address", pulumi.Output.from_input("cache.example"))  # noqa: B010
        with mock.patch.object(OLVaultK8SSecret, "__init__", recording_init):
            names, optional_names, resources = module.create_mitlearn_k8s_secrets(
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
                mitlearn_vault_mount=mitlearn_mount,
                db_config=SimpleNamespace(db_mount=db_mount),
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
            )
        result["env_from_secret_names"] = names
        result["optional_env_from_secret_names"] = optional_names
        result["resource_types"] = [type(r).__name__ for r in resources]
        return pulumi.Output.from_input(True)  # noqa: FBT003

    run()
    return {"vso_secrets": configs, **result}


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
