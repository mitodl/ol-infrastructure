"""Tests for OLApisixHTTPRoute's plugin handling.

A legacy ``ApisixRoute`` can name a shared ApisixPluginConfig *and* carry its
own plugins, and APISIX combines the two. A Gateway API HTTPRoute rule accepts
only one ExtensionRef filter, so ``OLApisixHTTPRoute`` has to do that merge
itself before writing the v1alpha1 PluginConfig. This module verifies:

1. Shared defaults and a route's own plugins both survive the merge
2. A route-level plugin replaces the same-named shared entry outright, which is
   what APISIX does on the legacy path
3. ``enable=False`` on a route plugin drops a shared plugin from that one route
4. Two routes sharing one config get their own PluginConfig, and each rule's
   ExtensionRef names the resource that was actually created
"""

from __future__ import annotations

import asyncio
from typing import Any

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class K8sMocks(pulumi.runtime.Mocks):
    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        return [args.name + "_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


pulumi.runtime.set_mocks(K8sMocks())


from ol_infrastructure.components.services.apisix import (  # noqa: E402
    OLApisixPluginConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.apisix_gateway_api import (  # noqa: E402
    OLApisixHTTPRoute,
    OLApisixHTTPRouteConfig,
)


def shared_plugins(name: str, **overrides) -> OLApisixSharedPlugins:
    return OLApisixSharedPlugins(
        name,
        plugin_config=OLApisixSharedPluginsConfig(
            application_name=name,
            k8s_namespace="myapp-ns",
            **overrides,
        ),
    )


def route(**overrides) -> OLApisixHTTPRouteConfig:
    return OLApisixHTTPRouteConfig(
        route_name=overrides.pop("route_name", "web"),
        hosts=["app.example.com"],
        paths=["/*"],
        backend_service_name="myapp",
        backend_service_port=8080,
        **overrides,
    )


def merged_names(route_config: OLApisixHTTPRouteConfig) -> list[str]:
    return [p.name for p in OLApisixHTTPRoute._merged_plugins(route_config)]


def merged_by_name(
    route_config: OLApisixHTTPRouteConfig,
) -> dict[str, dict[str, Any]]:
    return {p.name: p.config for p in OLApisixHTTPRoute._merged_plugins(route_config)}


# ─── the merge itself ──────────────────────────────────────────────────────────


@pulumi.runtime.test
def test_shared_and_route_plugins_both_survive():
    """The bug this replaced: setting the shared config silently discarded the
    route's own plugin list, including the request-id plugin the config class's
    own validator adds.
    """
    route_config = route(
        shared_plugins=shared_plugins("merge-both"),
        plugins=[
            OLApisixPluginConfig(
                name="mocking",
                secretRef=None,
                config={"response_status": 204, "response_example": ""},
            ),
        ],
    )
    names = merged_names(route_config)
    # From the shared config
    for expected in ("cors", "prometheus", "gzip", "redirect"):
        assert expected in names, expected
    # From the route, plus the validator's addition
    assert "mocking" in names
    assert "request-id" in names


@pulumi.runtime.test
def test_route_plugin_replaces_same_named_shared_plugin():
    """APISIX replaces rather than deep-merges a same-named plugin on the legacy
    path (verified in production on mitxonline's static-hash route, whose
    Cache-Control response-rewrite wins over the shared Referrer-Policy one).
    The Pulumi-side merge has to behave identically or moving a route from
    ApisixRoute to HTTPRoute would change its headers.
    """
    route_config = route(
        shared_plugins=shared_plugins("merge-override"),
        plugins=[
            OLApisixPluginConfig(
                name="response-rewrite",
                secretRef=None,
                config={"headers": {"set": {"Cache-Control": "private, no-cache"}}},
            ),
        ],
    )
    configs = merged_by_name(route_config)
    assert merged_names(route_config).count("response-rewrite") == 1
    headers = configs["response-rewrite"]["headers"]["set"]
    assert headers == {"Cache-Control": "private, no-cache"}
    assert "Referrer-Policy" not in headers


@pulumi.runtime.test
def test_route_can_opt_out_of_a_shared_plugin():
    """A disabled route-level entry replaces the shared one and is then dropped
    from the rendered v1alpha1 spec, which is the only per-route escape hatch
    now that every route gets the shared list merged in.
    """
    route_config = route(
        shared_plugins=shared_plugins("merge-optout"),
        plugins=[OLApisixPluginConfig(name="gzip", secretRef=None, enable=False)],
    )
    assert "gzip" in merged_names(route_config)
    active = OLApisixHTTPRoute._active_plugins(
        OLApisixHTTPRoute._merged_plugins(route_config)
    )
    assert "gzip" not in [p.name for p in active]
    # The rest of the shared list is untouched by the opt-out.
    assert "prometheus" in [p.name for p in active]


@pulumi.runtime.test
def test_request_id_survives_an_omitted_plugins_list():
    """Pydantic skips field validators for a field left at its default, so
    before ``validate_default`` a route that spelled out ``plugins=[]`` got
    request-id and an otherwise identical route that omitted the argument did
    not -- an invisible difference that decided whether a route's requests were
    correlatable.
    """
    assert merged_names(route()) == ["request-id"]
    assert "request-id" in merged_names(
        route(shared_plugins=shared_plugins("merge-default"))
    )


@pulumi.runtime.test
def test_request_id_default_is_not_accumulated_across_instances():
    """The validator now returns a new list instead of appending, because with
    validate_default=True an in-place append would grow the field's own default
    on every instantiation.
    """
    for _ in range(3):
        assert merged_names(route()) == ["request-id"]


@pulumi.runtime.test
def test_route_without_shared_plugins_keeps_its_own():
    """Routes that reference no shared config are unaffected by the merge."""
    route_config = route(
        plugins=[OLApisixPluginConfig(name="mocking", secretRef=None, config={})]
    )
    assert sorted(merged_names(route_config)) == ["mocking", "request-id"]


# ─── the rendered resources ────────────────────────────────────────────────────


@pulumi.runtime.test
def test_rule_extension_ref_names_the_created_plugin_config():
    """The ExtensionRef name is a hash of the merged plugin list, recomputed at
    render time rather than stored, so a route can end up pointing at a
    PluginConfig that was never created -- which APISIX resolves by serving the
    route with no plugins at all rather than by failing.

    The created names are read off the PluginConfig resources the component
    registered -- their own ``metadata.name``, the value that would reach the
    cluster -- rather than recomputed with the component's naming helper:
    recomputing them would compare the component against itself and still pass
    if it registered nothing at all.
    """
    plugins = shared_plugins("merge-render")
    route_configs = [
        route(
            route_name="static",
            shared_plugins=plugins,
            plugins=[
                OLApisixPluginConfig(
                    name="response-rewrite",
                    secretRef=None,
                    config={"headers": {"set": {"Cache-Control": "private"}}},
                ),
            ],
        ),
        route(route_name="passthrough", shared_plugins=plugins),
    ]
    component = OLApisixHTTPRoute(
        "merge-render-httproute",
        route_configs=route_configs,
        k8s_namespace="myapp-ns",
        k8s_labels={"app": "myapp"},
    )

    created = pulumi.Output.all(
        *[resource.metadata for resource in component.plugin_config_resources.values()]
    ).apply(lambda metadatas: {metadata["name"] for metadata in metadatas})

    def check(args):
        spec, created_names = args
        referenced = [
            rule["filters"][0]["extensionRef"]["name"] for rule in spec["rules"]
        ]
        assert len(referenced) == len(route_configs)
        # Every rule points at a PluginConfig that actually exists...
        assert set(referenced) <= created_names, set(referenced) - created_names
        # ...a distinct one per route, since the two routes' merged lists differ
        assert len(set(referenced)) == len(route_configs)
        # ...and not at the shared config itself, which is what a route
        # referenced before the merge moved into this component.
        assert plugins.resource_name not in referenced

        for rule in spec["rules"]:
            extension_ref = rule["filters"][0]["extensionRef"]
            # v1alpha1 PluginConfig -- the v2 ApisixPluginConfig kind is
            # silently ignored by the HTTPRoute reconciler.
            assert extension_ref["kind"] == "PluginConfig"
            assert extension_ref["group"] == "apisix.apache.org"

    return pulumi.Output.all(component.http_route_resource.spec, created).apply(check)
