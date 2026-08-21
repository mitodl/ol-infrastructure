"""Tests for the APISIX Pulumi components.

This module verifies:
1. OLApisixUpstreamConfig's chash field validation (hash_on/hash_key are
   required together with loadbalancer_type="chash", and rejected otherwise)
2. The rendered ApisixUpstream CRD spec for roundrobin vs chash
3. The ApisixUpstream resource is named after the target Service, per the
   apisix-ingress-controller name-matching contract documented on the class
4. OLApisixOIDCResources renders the flat lua-resty-session 4.x session.*
   keys the pinned APISIX expects, and omits them when unset
5. The session cookie names derived by bridge.lib.constants, and the stale
   cookie cleanup plugin's generated Lua
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from dataclasses import replace

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

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from bridge.lib.constants import (  # noqa: E402
    apisix_oidc_session_cookie_name,
    mit_learn_session_cookie_name,
)
from ol_infrastructure.components.services import apisix as apisix_module  # noqa: E402
from ol_infrastructure.components.services.apisix import (  # noqa: E402
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
    OLApisixUpstream,
    OLApisixUpstreamConfig,
    stale_session_cookie_cleanup_plugin,
)

# ─── OLApisixUpstreamConfig validation ─────────────────────────────────────────


def test_chash_requires_hash_on_and_hash_key():
    with pytest.raises(ValidationError, match="chash load balancing requires"):
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
            loadbalancer_type="chash",
        )


def test_chash_requires_hash_key_even_with_hash_on():
    with pytest.raises(ValidationError, match="chash load balancing requires"):
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
            loadbalancer_type="chash",
            hash_on="vars",
        )


def test_chash_with_hash_on_and_hash_key_ok():
    cfg = OLApisixUpstreamConfig(
        service_name="myapp-service",
        k8s_namespace="myapp-ns",
        loadbalancer_type="chash",
        hash_on="vars",
        hash_key="remote_addr",
    )
    assert cfg.hash_on == "vars"
    assert cfg.hash_key == "remote_addr"


def test_roundrobin_rejects_hash_on():
    with pytest.raises(ValidationError, match="only meaningful when"):
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
            loadbalancer_type="roundrobin",
            hash_on="vars",
        )


def test_roundrobin_rejects_hash_key():
    with pytest.raises(ValidationError, match="only meaningful when"):
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
            loadbalancer_type="roundrobin",
            hash_key="remote_addr",
        )


def test_default_loadbalancer_type_is_roundrobin():
    cfg = OLApisixUpstreamConfig(
        service_name="myapp-service",
        k8s_namespace="myapp-ns",
    )
    assert cfg.loadbalancer_type == "roundrobin"
    assert cfg.hash_on is None
    assert cfg.hash_key is None


# ─── Rendered ApisixUpstream CRD spec ──────────────────────────────────────────


@pulumi.runtime.test
def test_roundrobin_spec_has_no_hash_fields():
    """A roundrobin upstream's rendered spec must not carry hashOn/key."""
    upstream = OLApisixUpstream(
        "test-roundrobin-upstream",
        OLApisixUpstreamConfig(
            service_name="myapp-service",
            k8s_namespace="myapp-ns",
        ),
    )

    def check(spec):
        assert spec == {"loadbalancer": {"type": "roundrobin"}}

    return upstream.apisix_upstream_resource.spec.apply(check)


@pulumi.runtime.test
def test_chash_spec_has_hash_on_and_key():
    """A chash upstream's rendered spec must carry the configured hashOn/key."""
    upstream = OLApisixUpstream(
        "test-chash-upstream",
        OLApisixUpstreamConfig(
            service_name="cms-edxapp-app",
            k8s_namespace="mitx-openedx",
            loadbalancer_type="chash",
            hash_on="vars",
            hash_key="remote_addr",
        ),
    )

    def check(spec):
        assert spec == {
            "loadbalancer": {
                "type": "chash",
                "hashOn": "vars",
                "key": "remote_addr",
            }
        }

    return upstream.apisix_upstream_resource.spec.apply(check)


@pulumi.runtime.test
def test_resource_name_matches_service_name():
    """apisix-ingress-controller matches ApisixUpstream to a Service by exact
    same-name, same-namespace lookup -- metadata.name must equal service_name,
    not the Pulumi resource name.
    """
    upstream = OLApisixUpstream(
        "test-name-matching-upstream",
        OLApisixUpstreamConfig(
            service_name="cms-edxapp-app",
            k8s_namespace="mitx-openedx",
            loadbalancer_type="chash",
            hash_on="vars",
            hash_key="remote_addr",
        ),
    )

    def check(metadata):
        assert metadata["name"] == "cms-edxapp-app"
        assert metadata["namespace"] == "mitx-openedx"

    return upstream.apisix_upstream_resource.metadata.apply(check)


# ─── OLApisixOIDCResources session config ──────────────────────────────────────


def oidc_resources(name: str, **overrides) -> OLApisixOIDCResources:
    """Build an OIDC component with the fields every caller has to supply."""
    return OLApisixOIDCResources(
        name,
        oidc_config=OLApisixOIDCConfig(
            application_name="myapp",
            k8s_namespace="myapp-ns",
            vault_path="sso/myapp",
            vaultauth="myapp-vaultauth",
            **overrides,
        ),
    )


def test_session_cookie_name_is_emitted_flat():
    """lua-resty-session 4.x only reads the flat session.cookie_name key; the
    nested session.cookie.name form is a silent no-op on APISIX 3.17.0+.
    """
    oidc = oidc_resources(
        "test-oidc-cookie-name",
        oidc_session_cookie_name="mitlearn_apisix_session",
    )

    assert oidc.base_oidc_config["session"]["cookie_name"] == "mitlearn_apisix_session"


def test_session_cookie_name_and_domain_coexist():
    """Both flat keys land in the same session block rather than overwriting
    each other -- a broadened cookie domain is the reason the name matters.
    """
    oidc = oidc_resources(
        "test-oidc-cookie-name-and-domain",
        oidc_session_cookie_name="mitlearn_apisix_session",
        oidc_session_cookie_domain=".learn.mit.edu",
    )

    assert oidc.base_oidc_config["session"] == {
        "cookie_name": "mitlearn_apisix_session",
        "cookie_domain": ".learn.mit.edu",
    }


def test_session_block_omitted_when_nothing_set():
    """Callers that opt into none of the session settings must not get an empty
    session block, which would override lua-resty-session's own defaults.
    """
    oidc = oidc_resources("test-oidc-no-session-config")

    assert "session" not in oidc.base_oidc_config


def test_session_cookie_name_survives_plugin_rendering():
    """The name has to reach the actual plugin config attached to a route, not
    just the component's intermediate dict.
    """
    oidc = oidc_resources(
        "test-oidc-plugin-rendering",
        oidc_session_cookie_name="mitlearn_apisix_session",
    )

    plugin = oidc.get_full_oidc_plugin_config(unauth_action="pass")

    assert plugin["name"] == "openid-connect"
    assert plugin["config"]["session"]["cookie_name"] == "mitlearn_apisix_session"
    assert plugin["config"]["unauth_action"] == "pass"


# ─── Session cookie naming ─────────────────────────────────────────────────────


def test_production_cookie_name_has_no_env_suffix():
    assert apisix_oidc_session_cookie_name("mitlearn", "production") == (
        "mitlearn_apisix_session"
    )


@pytest.mark.parametrize(
    ("env_suffix", "expected"),
    [("qa", "mitlearn_apisix_session_qa"), ("ci", "mitlearn_apisix_session_ci")],
)
def test_non_production_cookie_names_are_suffixed(env_suffix, expected):
    """Suffixing the non-production names is what makes the unsuffixed
    Production name safe: a Production .learn.mit.edu cookie is also delivered
    to api.rc.learn.mit.edu, and must not be the name the RC gateway reads.
    """
    assert apisix_oidc_session_cookie_name("mitlearn", env_suffix) == expected


def test_cookie_name_normalises_hyphens():
    """Application slugs are hyphenated (jupyterhub-authoring); cookie names in
    this codebase are not.
    """
    assert apisix_oidc_session_cookie_name("jupyterhub-authoring", "production") == (
        "jupyterhub_authoring_apisix_session"
    )


def test_mit_learn_helper_matches_generic_helper():
    """Every resource sharing the MIT Learn session derives its name from
    mit_learn_session_cookie_name, so it must not drift from the generic form.
    """
    for env_suffix in ("production", "qa", "ci"):
        assert mit_learn_session_cookie_name(env_suffix) == (
            apisix_oidc_session_cookie_name("mitlearn", env_suffix)
        )


# ─── Stale session cookie cleanup ──────────────────────────────────────────────


def test_cleanup_plugin_runs_in_header_filter():
    """The default log phase is too late to mutate response headers."""
    plugin = stale_session_cookie_cleanup_plugin()

    assert plugin.name == "serverless-post-function"
    assert plugin.config["phase"] == "header_filter"


def test_cleanup_plugin_expires_host_only_variant_by_default():
    (lua,) = stale_session_cookie_cleanup_plugin().config["functions"]

    assert 'name == "session"' in lua
    assert "Max-Age=0" in lua
    assert "Domain=" not in lua


def test_cleanup_plugin_expires_each_named_domain_separately():
    """A cookie's identity is (name, domain, path), so the host-only and
    domain-scoped variants need one deletion each.
    """
    (lua,) = stale_session_cookie_cleanup_plugin(
        cookie_domains=[".learn.mit.edu"],
    ).config["functions"]

    assert lua.count("add_header") == 2
    assert "Domain=.learn.mit.edu" in lua


def test_cleanup_plugin_matches_chunked_cookies():
    """lua-resty-session splits an oversized payload across <name>, <name>_2,
    <name>_3... and each chunk is its own cookie to expire.
    """
    (lua,) = stale_session_cookie_cleanup_plugin().config["functions"]

    assert 'name:match("^session_%d+$")' in lua


def test_cleanup_plugin_honours_a_custom_stale_name():
    (lua,) = stale_session_cookie_cleanup_plugin(
        stale_cookie_name="mitlearn_apisix_session",
    ).config["functions"]

    assert 'name == "mitlearn_apisix_session"' in lua


# ─── Shared plugin defaults ────────────────────────────────────────────────────


def shared_plugins(name: str, **overrides) -> OLApisixSharedPlugins:
    """Build a shared plugin config with the fields every caller has to supply."""
    return OLApisixSharedPlugins(
        name,
        plugin_config=OLApisixSharedPluginsConfig(
            application_name="myapp",
            k8s_namespace="myapp-ns",
            **overrides,
        ),
    )


def plugin_named(plugins, name):
    """Return the single plugin entry called ``name``, or None if absent."""
    matches = [plugin for plugin in plugins if plugin["name"] == name]
    assert len(matches) <= 1, f"{name} rendered more than once"
    return matches[0] if matches else None


@contextmanager
def stack_env(env_suffix: str):
    """Pretend the component is being rendered against a given environment.

    ``parse_stack`` is imported into the apisix module's namespace, and the
    mocks fix the stack name process-wide, so patching the reference there is
    the only way to exercise the per-environment gate.
    """
    original = apisix_module.parse_stack
    apisix_module.parse_stack = lambda: replace(original(), env_suffix=env_suffix)
    try:
        yield
    finally:
        apisix_module.parse_stack = original


@pulumi.runtime.test
def test_gzip_is_attached_outside_production():
    """APISIX loads the gzip plugin cluster-wide, but a plugin does nothing
    until a route or plugin config references it -- for a long time this one
    referenced it nowhere and every shared-gateway response went out
    uncompressed. Non-production environments soak the fix first.
    """
    with stack_env("qa"):
        plugins = shared_plugins("test-shared-plugins-gzip-qa")

    def check(spec):
        gzip = plugin_named(spec["plugins"], "gzip")
        assert gzip is not None
        assert gzip["enable"] is True

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_is_absent_in_production_by_default():
    """Production stays off until the non-production soak says otherwise. The
    risk is not correctness -- APISIX loads gzip everywhere -- it is CPU on a
    gateway whose HPA scales on CPU, which only shows up at production volume.
    """
    with stack_env("production"):
        plugins = shared_plugins("test-shared-plugins-gzip-production")

    def check(spec):
        assert plugin_named(spec["plugins"], "gzip") is None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_can_be_forced_on_in_production():
    """An application that has done its own measurement can go early without
    waiting for the fleet-wide default to flip.
    """
    with stack_env("production"):
        plugins = shared_plugins(
            "test-shared-plugins-gzip-production-override",
            enable_gzip=True,
        )

    def check(spec):
        assert plugin_named(spec["plugins"], "gzip") is not None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_can_be_forced_off_outside_production():
    """The override has to work in both directions -- an app that streams, or
    is otherwise a bad fit for compression, opts out of the soak too.
    """
    with stack_env("qa"):
        plugins = shared_plugins(
            "test-shared-plugins-gzip-qa-opt-out",
            enable_gzip=False,
        )

    def check(spec):
        assert plugin_named(spec["plugins"], "gzip") is None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_is_independent_of_enable_defaults():
    """Gzip carries its own flag rather than riding in __default_plugins, so
    enable_defaults=False does not turn it off -- same contract as
    opentelemetry. enable_gzip=False is the way to drop it.
    """
    with stack_env("qa"):
        plugins = shared_plugins(
            "test-shared-plugins-gzip-without-defaults",
            enable_defaults=False,
        )

    def check(spec):
        assert plugin_named(spec["plugins"], "gzip") is not None
        # The actual defaults are gone, confirming the flag is doing the work.
        assert plugin_named(spec["plugins"], "cors") is None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_reaches_the_gateway_api_plugin_config():
    """The v1alpha1 PluginConfig is rendered by a separate comprehension that
    rewrites each entry, so Gateway API HTTPRoutes need their own assertion
    rather than inheriting the v2 one.
    """
    plugins = shared_plugins("test-shared-plugins-gzip-gateway-api")

    def check(spec):
        gzip = plugin_named(spec["plugins"], "gzip")
        assert gzip is not None
        # v1alpha1 accepts only name and config -- ``enable`` is v2-only.
        assert set(gzip) == {"name", "config"}

    return plugins.shared_plugin_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_does_not_compress_streaming_or_precompressed_types():
    """text/event-stream is excluded so SSE responses are not held back by the
    compression buffers, and already-compressed formats are excluded so they
    do not burn gateway CPU for no gain. Both are easy to undo by accident
    when someone widens the list.
    """
    plugins = shared_plugins("test-shared-plugins-gzip-types")

    def check(spec):
        types = plugin_named(spec["plugins"], "gzip")["config"]["types"]
        assert "text/event-stream" not in types
        for precompressed in (
            "image/png",
            "video/mp4",
            "font/woff2",
            "application/zip",
        ):
            assert precompressed not in types

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_compression_level_stays_cheap():
    """comp_level is pinned to NGINX's own default of 1 on purpose: this
    attaches to every route on a gateway whose HPA scales on CPU. Raising it
    is a deliberate decision to make with measurement in hand, not a drive-by.
    """
    plugins = shared_plugins("test-shared-plugins-gzip-comp-level")

    def check(spec):
        config = plugin_named(spec["plugins"], "gzip")["config"]
        assert config["comp_level"] == 1
        assert config["vary"] is True

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


# ─── Rate limiting ──────────────────────────────────────────────────────────────


@pulumi.runtime.test
def test_rate_limiting_is_absent_by_default():
    """enable_rate_limiting defaults to off so that turning it on for one
    application does not change behaviour for other services sharing this
    component.
    """
    plugins = shared_plugins("test-shared-plugins-ratelimit-default")

    def check(spec):
        assert plugin_named(spec["plugins"], "limit-conn") is None
        assert plugin_named(spec["plugins"], "limit-req") is None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_rate_limiting_emits_both_plugins_on_apisix_pluginconfig():
    """Opting in attaches both limit-conn (concurrency) and limit-req
    (request rate) to the legacy v2 ApisixPluginConfig CRD.
    """
    plugins = shared_plugins(
        "test-shared-plugins-ratelimit-v2", enable_rate_limiting=True
    )

    def check(spec):
        assert plugin_named(spec["plugins"], "limit-conn") is not None
        assert plugin_named(spec["plugins"], "limit-req") is not None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_rate_limiting_emits_both_plugins_on_gateway_api_pluginconfig():
    """The v1alpha1 PluginConfig is rendered by a separate comprehension, so
    Gateway API HTTPRoutes need their own assertion rather than inheriting
    the v2 one -- same contract as gzip.
    """
    plugins = shared_plugins(
        "test-shared-plugins-ratelimit-v1alpha1", enable_rate_limiting=True
    )

    def check(spec):
        assert plugin_named(spec["plugins"], "limit-conn") is not None
        assert plugin_named(spec["plugins"], "limit-req") is not None

    return plugins.shared_plugin_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_rate_limiting_custom_thresholds_propagate():
    """Custom thresholds reach the rendered plugin config rather than the
    defaults silently winning.
    """
    plugins = shared_plugins(
        "test-shared-plugins-ratelimit-custom",
        enable_rate_limiting=True,
        rate_limit_key="consumer_name",
        rate_limit_rejected_code=503,
        rate_limit_requests_per_second=10,
        rate_limit_burst=5,
        rate_limit_max_concurrent=20,
        rate_limit_concurrent_burst=10,
    )

    def check(spec):
        limit_conn = plugin_named(spec["plugins"], "limit-conn")["config"]
        assert limit_conn["conn"] == 20
        assert limit_conn["burst"] == 10
        assert limit_conn["key"] == "consumer_name"
        assert limit_conn["rejected_code"] == 503

        limit_req = plugin_named(spec["plugins"], "limit-req")["config"]
        assert limit_req["rate"] == 10
        assert limit_req["burst"] == 5
        assert limit_req["key"] == "consumer_name"
        assert limit_req["rejected_code"] == 503

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


def test_rate_limit_rejected_code_rejects_out_of_range():
    with pytest.raises(ValidationError):
        OLApisixSharedPluginsConfig(
            application_name="myapp",
            k8s_namespace="myapp-ns",
            rate_limit_rejected_code=100,
        )


@pytest.mark.parametrize(
    "field",
    [
        "rate_limit_requests_per_second",
        "rate_limit_max_concurrent",
    ],
)
def test_rate_limit_positive_fields_reject_non_positive(field):
    with pytest.raises(ValidationError):
        OLApisixSharedPluginsConfig(
            application_name="myapp",
            k8s_namespace="myapp-ns",
            **{field: 0},
        )


@pytest.mark.parametrize(
    "field",
    [
        "rate_limit_burst",
        "rate_limit_concurrent_burst",
    ],
)
def test_rate_limit_burst_fields_reject_negative(field):
    with pytest.raises(ValidationError):
        OLApisixSharedPluginsConfig(
            application_name="myapp",
            k8s_namespace="myapp-ns",
            **{field: -1},
        )
