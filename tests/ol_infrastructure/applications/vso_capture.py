"""Record the VSO resources an app's k8s_secrets.py creates, for golden tests.

Shared by the per-app ``test_k8s_secrets.py`` golden tests (mitxonline,
mit_learn): each pins what its deployed program's VaultStaticSecret and
VaultDynamicSecret resources are configured with.
"""

import asyncio
from collections.abc import Callable
from typing import Any, NamedTuple
from unittest import mock

import pulumi
import pulumi_vault as vault

from ol_infrastructure.components.services.vault import OLVaultK8SSecret

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class MountSpec(NamedTuple):
    """A Vault mount to create, and the marker its Output path is recorded as."""

    path: str
    type: str
    marker: str


class _Mocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [f"{args.name}-id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def capture_vso_secrets(
    *,
    project: str,
    stack: str,
    mounts: dict[str, MountSpec],
    create: Callable[[dict[str, vault.Mount], vault.Mount], tuple[Any, Any, Any]],
) -> dict[str, Any]:
    """Run ``create`` under mocks and record every OLVaultK8SSecret it makes.

    :param mounts: Vault mounts to create, by name; ``create`` receives them.
    :param create: Calls the app's ``create_*_k8s_secrets`` with the mounts and
        a cache stand-in, returning its (envFrom names, optional names,
        resources).
    :returns: Each OLVaultK8SSecret's Pulumi name and config (templates as
        ordered pairs; a mount created here as its marker), the Secret names in
        the order pods list them under ``envFrom``, the names pods mount as
        optional, and the type of each returned resource.
    """
    pulumi.runtime.set_mocks(_Mocks(), project=project, stack=stack, preview=False)
    configs: list[dict[str, Any]] = []
    markers: dict[int, str] = {}
    original_init = OLVaultK8SSecret.__init__

    def recording_init(self, name, resource_config, opts=None):
        mount = resource_config.mount
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
        created = {
            name: vault.Mount(name, path=spec.path, type=spec.type)
            for name, spec in mounts.items()
        }
        markers.update(
            {id(created[name].path): spec.marker for name, spec in mounts.items()}
        )
        cache = vault.Mount("cache", path="cache", type="kv")
        # The redis Secret reads only .address; any Resource satisfies depends_on.
        setattr(cache, "address", pulumi.Output.from_input("cache.example"))  # noqa: B010
        with mock.patch.object(OLVaultK8SSecret, "__init__", recording_init):
            names, optional_names, resources = create(created, cache)
        result["env_from_secret_names"] = names
        result["optional_env_from_secret_names"] = optional_names
        result["resource_types"] = [type(r).__name__ for r in resources]
        return pulumi.Output.from_input(True)  # noqa: FBT003

    run()
    return {"vso_secrets": configs, **result}
