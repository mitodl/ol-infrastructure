"""Pin the VSO resources mitxonline's deployed program creates.

``vso_secrets_main.json`` was generated once by ``capture`` from k8s_secrets.py
as it stood on main at dd285f077, before the variables were described as data.
It proves the description reproduces that file; a difference here is a change
to what deployed stacks get, so regenerate the fixture only on purpose.
"""

import asyncio
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest import mock

import pulumi
import pulumi_vault as vault

from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultRestartTarget,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.secret_env import vso_template

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

FIXTURE = Path(__file__).parent / "fixtures" / "vso_secrets_main.json"
DEPLOYMENTS = ["mitxonline-app", "mitxonline-celery-beat"]


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def capture(module: ModuleType, starrocks_mount: str | None = None) -> dict[str, Any]:
    """Call ``module.create_mitxonline_k8s_secrets`` with QA-shaped inputs.

    :param starrocks_mount: Passed through; QA, which the fixture records, has none.
    :returns: Each OLVaultK8SSecret's Pulumi name and config (templates as
        ordered pairs; the database mount, an Output, as a marker), the Secret
        names in the order pods list them under ``envFrom``, the names pods
        mount as optional, and the type of each returned resource.
    """
    pulumi.runtime.set_mocks(
        _Mocks(), project="ol-application-mitxonline", stack="QA", preview=False
    )
    configs: list[dict[str, Any]] = []
    original_init = OLVaultK8SSecret.__init__

    def recording_init(self, name, resource_config, opts=None):
        mount = resource_config.mount
        configs.append(
            {
                "resource_name": name,
                "config_class": type(resource_config).__name__,
                "mount": "<database mount>" if mount is state["db_path"] else mount,
                "templates": [list(item) for item in resource_config.templates.items()],
                **resource_config.model_dump(
                    mode="json", exclude={"mount", "templates"}
                ),
            }
        )
        original_init(self, name, resource_config, opts)

    result: dict[str, Any] = {}
    state: dict[str, Any] = {}

    @pulumi.runtime.test
    def run():
        db_mount = vault.Mount("db", path="postgres-mitxonline", type="database")
        state["db_path"] = db_mount.path
        cache = vault.Mount("cache", path="cache", type="kv")
        # The redis Secret reads only .address; any Resource satisfies depends_on.
        setattr(cache, "address", pulumi.Output.from_input("cache.example"))  # noqa: B010
        with mock.patch.object(OLVaultK8SSecret, "__init__", recording_init):
            names, optional_names, resources = module.create_mitxonline_k8s_secrets(
                stack_info=StackInfo(
                    name="QA",
                    namespace="applications.mitxonline",
                    env_suffix="qa",
                    env_prefix="applications",
                    full_name="applications.mitxonline.QA",
                    k8s_name="qa",
                ),
                mitxonline_namespace="mitxonline",
                k8s_global_labels={"ol.mit.edu/application": "mitxonline"},
                vault_k8s_resources=SimpleNamespace(auth_name="mitxonline"),
                db_config=SimpleNamespace(db_mount=db_mount),
                rds_endpoint="mitxonline-db.example:5432",
                openedx_environment="mitxonline-qa",
                redis_password="redis-password",  # pragma: allowlist secret
                redis_cache=cache,
                starrocks_mount=starrocks_mount,
                starrocks_restart_targets=[
                    OLVaultRestartTarget(kind="Deployment", name=name)
                    for name in DEPLOYMENTS
                ],
            )
        result["env_from_secret_names"] = names
        result["optional_env_from_secret_names"] = optional_names
        result["resource_types"] = [type(r).__name__ for r in resources]
        return pulumi.Output.from_input(True)  # noqa: FBT003

    run()
    return {"vso_secrets": configs, **result}


def test_the_vso_resources_match_main_before_the_description():
    # Imported here, not at module level: k8s_secrets pulls in an ElastiCache
    # helper that builds a boto3 client on import, and conftest only sets
    # AWS_REGION once collection is over.
    from ol_infrastructure.applications.mitxonline import (  # noqa: PLC0415
        k8s_secrets,
    )

    captured = capture(k8s_secrets)
    assert captured.pop("optional_env_from_secret_names") == []
    assert captured == json.loads(FIXTURE.read_text())


def test_starrocks_credentials_are_an_optional_dynamic_secret():
    """With a StarRocks mount, the read-only credentials are one more dynamic
    secret, which pods mount as optional so it cannot hold up a rollout, and
    whose rotation restarts the Deployments, which read it through envFrom.
    """
    from ol_infrastructure.applications.mitxonline import (  # noqa: PLC0415
        k8s_secrets,
    )

    without = capture(k8s_secrets)
    captured = capture(k8s_secrets, starrocks_mount="database-starrocks")

    assert captured["optional_env_from_secret_names"] == [
        "starrocks-mitxonline-dynamic-secret"
    ]
    assert captured["env_from_secret_names"] == without["env_from_secret_names"]
    [starrocks] = [
        config
        for config in captured["vso_secrets"]
        if config["mount"] == "database-starrocks"
    ]
    assert starrocks["config_class"] == "OLVaultK8SDynamicSecretConfig"
    assert starrocks["path"] == "creds/readonly"
    assert starrocks["templates"] == [
        ["STARROCKS_USER", vso_template(("username",))],
        ["STARROCKS_PASSWORD", vso_template(("password",))],
    ]
    assert starrocks["restart_targets"] == [
        {"kind": "Deployment", "name": name} for name in DEPLOYMENTS
    ]
    assert [
        config["resource_name"]
        for config in captured["vso_secrets"]
        if config["restart_targets"]
    ] == [starrocks["resource_name"]]
