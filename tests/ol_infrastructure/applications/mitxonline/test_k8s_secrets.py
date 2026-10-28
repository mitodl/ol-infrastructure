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

from ol_infrastructure.applications.mitxonline import k8s_secrets
from ol_infrastructure.components.services.vault import OLVaultK8SSecret
from ol_infrastructure.lib.pulumi_helper import StackInfo

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

FIXTURE = Path(__file__).parent / "fixtures" / "vso_secrets_main.json"


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def capture(module: ModuleType) -> dict[str, Any]:
    """Call ``module.create_mitxonline_k8s_secrets`` with QA-shaped inputs.

    :returns: Each OLVaultK8SSecret's Pulumi name and config (templates as
        ordered pairs; the database mount, an Output, as a marker), the Secret
        names in the order pods list them under ``envFrom``, and the type of
        each returned resource.
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
            names, resources = module.create_mitxonline_k8s_secrets(
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
            )
        result["env_from_secret_names"] = names
        result["resource_types"] = [type(r).__name__ for r in resources]
        return pulumi.Output.from_input(True)  # noqa: FBT003

    run()
    return {"vso_secrets": configs, **result}


def test_the_vso_resources_match_main_before_the_description():
    assert capture(k8s_secrets) == json.loads(FIXTURE.read_text())
