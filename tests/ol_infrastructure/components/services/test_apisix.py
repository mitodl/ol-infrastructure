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

import ast
import asyncio
import collections
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

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
    OLApisixSharedPluginsVariant,
    OLApisixUpstream,
    OLApisixUpstreamConfig,
    oidc_gateway_pre_function_plugin,
    ol_apisix_shared_plugins_variants,
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


# ─── OIDC error callback recovery ──────────────────────────────────────────────


def test_recovery_plugin_runs_in_rewrite_before_openid_connect():
    """openid-connect runs in rewrite; the access phase would be too late."""
    plugin = oidc_gateway_pre_function_plugin()

    assert plugin.name == "serverless-pre-function"
    assert plugin.config["phase"] == "rewrite"


def test_recovery_plugin_defaults_to_the_only_error_production_emits():
    """access_denied means the user pressed Cancel -- restarting the flow there
    would bounce the browser between the gateway and Keycloak.
    """
    options = oidc_gateway_pre_function_plugin().config["oidc_error_recovery"]

    assert options["recoverable_errors"] == ["temporarily_unavailable"]


def test_recovery_plugin_honours_a_custom_error_list():
    options = oidc_gateway_pre_function_plugin(
        recoverable_errors=["temporarily_unavailable", "server_error"],
    ).config["oidc_error_recovery"]

    assert options["recoverable_errors"] == ["temporarily_unavailable", "server_error"]


def test_recovery_plugin_honours_an_explicit_empty_error_list():
    """An empty list means "recover nothing" -- the way to make the plugin a
    no-op without detaching it from every route on a shared config.
    """
    options = oidc_gateway_pre_function_plugin(
        recoverable_errors=[],
    ).config["oidc_error_recovery"]

    assert options["recoverable_errors"] == []


def test_recovery_plugin_passes_guard_settings_as_config():
    """Tunables travel on the plugin config and are read off ``conf`` in Lua,
    so nothing is interpolated into the shipped source.
    """
    options = oidc_gateway_pre_function_plugin(
        guard_cookie_name="custom_guard",
        guard_max_age=90,
    ).config["oidc_error_recovery"]

    assert options["guard_cookie_name"] == "custom_guard"
    assert options["guard_max_age"] == 90


def test_recovery_plugin_ships_the_lua_files_verbatim():
    """The function bodies are the checked-in .lua files, not generated strings --
    no configuration is interpolated into either.
    """
    sources = oidc_gateway_pre_function_plugin(
        guard_cookie_name="custom_guard",
        recoverable_errors=["server_error"],
        canonical_redirect_status=301,
    ).config["functions"]

    assert sources == [
        apisix_module.CANONICAL_HTTPS_REDIRECT_LUA,
        apisix_module.OIDC_ERROR_RECOVERY_LUA,
    ]
    for source in sources:
        assert "custom_guard" not in source
        assert "server_error" not in source


def test_canonical_redirect_runs_before_error_recovery():
    """serverless/init.lua stops at the first function returning a code, so the
    origin has to be canonical before the recovery function can redirect back
    into a login flow -- otherwise recovery would target an http:// origin.
    """
    sources = oidc_gateway_pre_function_plugin().config["functions"]

    assert "canonical_https_redirect" in sources[0]
    assert "oidc_error_recovery" in sources[1]


def test_canonical_redirect_status_reaches_the_config_block():
    config = oidc_gateway_pre_function_plugin(canonical_redirect_status=301).config

    assert config["canonical_https_redirect"]["status"] == 301


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_every_status_ngx_redirect_accepts_is_allowed(status):
    config = oidc_gateway_pre_function_plugin(canonical_redirect_status=status).config

    assert config["canonical_https_redirect"]["status"] == status


@pytest.mark.parametrize("status", [200, 304, 305, 418, 500])
def test_a_status_ngx_redirect_rejects_fails_at_preview(status):
    """ngx.redirect raises a Lua error outside {301,302,303,307,308}, and the
    config block carrying this is not in serverless-pre-function's schema, so
    APISIX would not reject it either -- an unchecked value would first surface
    as a 500 on live traffic.  This has to fail while the stack is being built.
    """
    with pytest.raises(ValueError, match=r"ngx\.redirect rejects anything else"):
        oidc_gateway_pre_function_plugin(canonical_redirect_status=status)


def test_canonical_redirect_can_be_disabled():
    """A host that must keep answering on plain HTTP drops the function without
    losing the error-callback recovery it necessarily shares a plugin with.
    """
    config = oidc_gateway_pre_function_plugin(canonical_https_redirect=False).config

    assert config["functions"] == [apisix_module.OIDC_ERROR_RECOVERY_LUA]


def test_canonical_redirect_lua_reads_its_settings_off_conf():
    """Guards the contract between the .lua file and the config block above."""
    source = apisix_module.CANONICAL_HTTPS_REDIRECT_LUA

    assert "conf.canonical_https_redirect" in source
    assert "opts.status" in source


def test_recovery_lua_reads_its_settings_off_conf():
    """Guards the contract between the .lua file and the config block above."""
    source = apisix_module.OIDC_ERROR_RECOVERY_LUA

    assert "conf.oidc_error_recovery" in source
    assert "opts.recoverable_errors" in source
    assert "opts.guard_cookie_name" in source
    assert "opts.guard_max_age" in source


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
def test_gzip_is_attached_by_default():
    """APISIX loads the gzip plugin cluster-wide, but a plugin does nothing
    until a route or plugin config references it -- for a long time this one
    referenced it nowhere and every shared-gateway response went out
    uncompressed. Attaching it is the default now.
    """
    with stack_env("qa"):
        plugins = shared_plugins("test-shared-plugins-gzip-qa")

    def check(spec):
        gzip = plugin_named(spec["plugins"], "gzip")
        assert gzip is not None
        assert gzip["enable"] is True

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_is_attached_in_production():
    """Production was gated behind a non-production soak because the risk was
    never correctness -- APISIX loads gzip everywhere -- but CPU on a gateway
    whose HPA scales on CPU. The soak plus a measurement against real peak
    egress retired that gate, so Production is no longer a special case.
    """
    with stack_env("production"):
        plugins = shared_plugins("test-shared-plugins-gzip-production")

    def check(spec):
        assert plugin_named(spec["plugins"], "gzip") is not None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_gzip_can_be_forced_off_in_production():
    """The opt-out has to reach Production, since that is where an application
    that streams incrementally under a compressible content type would actually
    be hurt by the compression buffers.
    """
    with stack_env("production"):
        plugins = shared_plugins(
            "test-shared-plugins-gzip-production-opt-out",
            enable_gzip=False,
        )

    def check(spec):
        assert plugin_named(spec["plugins"], "gzip") is None

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
def test_recovery_plugin_renders_into_the_v2_plugin_config():
    """The applications attach this to a host's shared plugin config rather
    than per route, so it has to survive that normalisation.
    """
    plugins = shared_plugins(
        "test-shared-plugins-oidc-recovery-v2",
        plugins=[oidc_gateway_pre_function_plugin()],
    )

    def check(spec):
        recovery = plugin_named(spec["plugins"], "serverless-pre-function")
        assert recovery is not None
        assert recovery["config"]["phase"] == "rewrite"
        # The settings block is not part of serverless-pre-function's schema.
        # It reaches the gateway because the CRD marks config
        # x-kubernetes-preserve-unknown-fields, the controller holds it as raw
        # apiextensionsv1.JSON, ADC as map[string]any, and APISIX's serverless
        # schema does not set additionalProperties.  If a future version
        # tightens any of those, this is the assertion that should fail first.
        assert recovery["config"]["oidc_error_recovery"] == {
            "recoverable_errors": ["temporarily_unavailable"],
            "guard_cookie_name": "apisix_oidc_recovery",
            "guard_max_age": 60,
        }

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_recovery_plugin_reaches_the_gateway_api_plugin_config():
    """v1alpha1 drops secretRef, which this plugin sets to None -- a shape the
    other shared plugins do not exercise.
    """
    plugins = shared_plugins(
        "test-shared-plugins-oidc-recovery-gateway-api",
        plugins=[oidc_gateway_pre_function_plugin()],
    )

    def check(spec):
        recovery = plugin_named(spec["plugins"], "serverless-pre-function")
        assert recovery is not None
        assert set(recovery) == {"name", "config"}

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


# ─── enable_cors ───────────────────────────────────────────────────────────────

# The other three defaults, which enable_cors=False must leave untouched.
_NON_CORS_DEFAULTS = ("redirect", "response-rewrite", "prometheus")


@pulumi.runtime.test
def test_cors_is_attached_by_default():
    """Documents what the default actually grants: allow_origins "**" with
    allow_credential True is not the credentialless `Access-Control-Allow-Origin:
    *` it reads like -- APISIX reflects the request Origin and the browser will
    hand over cookies. Anything that narrows this default should have to change
    this assertion on purpose.
    """
    plugins = shared_plugins("test-shared-plugins-cors-default")

    def check(spec):
        cors = plugin_named(spec["plugins"], "cors")
        assert cors is not None
        assert cors["enable"] is True
        assert cors["config"]["allow_origins"] == "**"
        assert cors["config"]["allow_credential"] is True

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_cors_can_be_disabled():
    """An internal tool referencing this config for prometheus/otel/gzip must
    not also pick up a browser-facing origin grant.
    """
    plugins = shared_plugins(
        "test-shared-plugins-cors-off",
        enable_cors=False,
    )

    def check(spec):
        assert plugin_named(spec["plugins"], "cors") is None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_cors_disabled_removes_only_cors():
    """The filter runs over the rendered list rather than lifting cors out of
    __default_plugins, so a bad predicate would silently take the neighbouring
    defaults with it -- and losing prometheus or response-rewrite this way
    would show up as missing metrics, not as an error.
    """
    plugins = shared_plugins(
        "test-shared-plugins-cors-off-only-cors",
        enable_cors=False,
    )

    def check(spec):
        for name in _NON_CORS_DEFAULTS:
            assert plugin_named(spec["plugins"], name) is not None, name
        assert plugin_named(spec["plugins"], "gzip") is not None

    return plugins.shared_plugin_apisix_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_cors_reaches_the_gateway_api_plugin_config():
    """v1alpha1 is rendered by its own comprehension over the same list, so the
    Gateway API path needs its own assertion rather than inheriting the v2 one.
    """
    plugins = shared_plugins("test-shared-plugins-cors-gateway-api")

    def check(spec):
        cors = plugin_named(spec["plugins"], "cors")
        assert cors is not None
        # v1alpha1 accepts only name and config -- ``enable`` is v2-only.
        assert set(cors) == {"name", "config"}
        assert cors["config"]["allow_credential"] is True

    return plugins.shared_plugin_pluginconfig_resource.spec.apply(check)


@pulumi.runtime.test
def test_cors_disabled_reaches_the_gateway_api_plugin_config():
    """The opt-out has to hold on both CRDs: an application that passes
    enable_cors=False and is later moved from a legacy ApisixRoute to an
    HTTPRoute would otherwise get the origin grant back on the way across.
    """
    plugins = shared_plugins(
        "test-shared-plugins-cors-off-gateway-api",
        enable_cors=False,
    )

    def check(spec):
        assert plugin_named(spec["plugins"], "cors") is None
        for name in _NON_CORS_DEFAULTS:
            assert plugin_named(spec["plugins"], name) is not None, name

    return plugins.shared_plugin_pluginconfig_resource.spec.apply(check)


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


# ─── Shared plugin variants ─────────────────────────────────────────────────────


def learn_shaped_variants():
    """Two variants on one host, shaped like api.learn.mit.edu's."""
    return ol_apisix_shared_plugins_variants(
        plugin_config=OLApisixSharedPluginsConfig(
            application_name="myapp",
            k8s_namespace="myapp-ns",
            plugins=[oidc_gateway_pre_function_plugin()],
        ),
        variants=[
            OLApisixSharedPluginsVariant(
                name="test-variants-base",
                resource_suffix="ol-shared-plugins",
            ),
            OLApisixSharedPluginsVariant(
                name="test-variants-browser",
                resource_suffix="ol-browser-shared-plugins",
                enable_rate_limiting=True,
            ),
        ],
    )


@pulumi.runtime.test
def test_variants_render_the_same_plugins_apart_from_rate_limiting():
    """The whole point of the factory. Two hand-written configs on one host
    diverge silently -- a plugin on only one of them changes behaviour by
    request Origin, and it has shipped that way twice on api.learn. Rate
    limiting is the one difference a variant is allowed to carry.
    """
    variants = learn_shaped_variants()
    rate_limit_plugins = {"limit-conn", "limit-req"}

    def check(specs):
        base, browser = specs
        base_names = [plugin["name"] for plugin in base["plugins"]]
        browser_names = [
            plugin["name"]
            for plugin in browser["plugins"]
            if plugin["name"] not in rate_limit_plugins
        ]
        assert base_names, "nothing was compared"
        assert base_names == browser_names
        assert rate_limit_plugins.isdisjoint(base_names)
        assert rate_limit_plugins.issubset(
            {plugin["name"] for plugin in browser["plugins"]}
        )

    return pulumi.Output.all(
        variants["ol-shared-plugins"].shared_plugin_apisix_pluginconfig_resource.spec,
        variants[
            "ol-browser-shared-plugins"
        ].shared_plugin_apisix_pluginconfig_resource.spec,
    ).apply(check)


@pulumi.runtime.test
def test_variants_render_the_same_plugins_on_gateway_api_pluginconfig():
    """The v1alpha1 PluginConfig is built by its own comprehension, so it needs
    its own assertion rather than inheriting the v2 one.
    """
    variants = learn_shaped_variants()

    rate_limit_plugins = {"limit-conn", "limit-req"}

    def check(specs):
        base, browser = specs
        base_names = [plugin["name"] for plugin in base["plugins"]]
        browser_names = [
            plugin["name"]
            for plugin in browser["plugins"]
            if plugin["name"] not in rate_limit_plugins
        ]
        assert base_names, "nothing was compared"
        assert base_names == browser_names
        # Without these the filter above turns into a no-op the moment the
        # v1alpha1 comprehension stops emitting the rate-limit plugins, and
        # this test stays green while every Gateway API browser route loses
        # its rate limiting.
        assert rate_limit_plugins.isdisjoint(base_names)
        assert rate_limit_plugins.issubset(
            {plugin["name"] for plugin in browser["plugins"]}
        )

    return pulumi.Output.all(
        variants["ol-shared-plugins"].shared_plugin_pluginconfig_resource.spec,
        variants["ol-browser-shared-plugins"].shared_plugin_pluginconfig_resource.spec,
    ).apply(check)


def test_variants_keep_distinct_crd_names():
    """Routes reference a variant by the CRD metadata.name that
    resource_suffix produces, so the suffix has to reach the component.
    """
    variants = learn_shaped_variants()
    assert variants["ol-shared-plugins"].resource_name == "myapp-ol-shared-plugins"
    assert (
        variants["ol-browser-shared-plugins"].resource_name
        == "myapp-ol-browser-shared-plugins"
    )


def test_variants_reject_a_duplicate_resource_suffix():
    """Both CRDs would be created under one metadata.name and the second would
    win, which is a silent swap of a host's plugin list.
    """
    with pytest.raises(ValueError, match="distinct resource_suffix"):
        ol_apisix_shared_plugins_variants(
            plugin_config=OLApisixSharedPluginsConfig(
                application_name="myapp",
                k8s_namespace="myapp-ns",
            ),
            variants=[
                OLApisixSharedPluginsVariant(
                    name="test-variants-dupe-a", resource_suffix="same"
                ),
                OLApisixSharedPluginsVariant(
                    name="test-variants-dupe-b", resource_suffix="same"
                ),
            ],
        )


@pytest.mark.parametrize(
    "field",
    [
        "enable_rate_limiting",
        "resource_suffix",
    ],
)
def test_variants_reject_per_variant_fields_on_the_shared_config(field):
    """Converting a single-config application to the factory means moving
    these onto a variant. Left behind on the shared config they would be
    silently discarded, which for enable_rate_limiting means dropping rate
    limiting from every route on the host with nothing to show for it.
    """
    values = {"enable_rate_limiting": True, "resource_suffix": "ol-shared-plugins"}
    with pytest.raises(ValueError, match="belong to a variant"):
        ol_apisix_shared_plugins_variants(
            plugin_config=OLApisixSharedPluginsConfig(
                application_name="myapp",
                k8s_namespace="myapp-ns",
                **{field: values[field]},
            ),
            variants=[
                OLApisixSharedPluginsVariant(
                    name="test-variants-misplaced",
                    resource_suffix="ol-shared-plugins",
                ),
            ],
        )


def test_variant_cannot_carry_its_own_plugin_list():
    """``extra="forbid"`` is what makes the shared list structural: a
    per-variant ``plugins`` is an error rather than a silently ignored field.
    """
    with pytest.raises(ValidationError):
        OLApisixSharedPluginsVariant(
            name="test-variant-own-plugins",
            resource_suffix="ol-shared-plugins",
            plugins=[oidc_gateway_pre_function_plugin()],
        )


SHARED_PLUGINS_CLASS = "OLApisixSharedPlugins"


def _local_names_for(tree):
    """Return every name OLApisixSharedPlugins is bound to in this module."""
    names = {SHARED_PLUGINS_CLASS}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.update(
                alias.asname
                for alias in node.names
                if alias.name == SHARED_PLUGINS_CLASS and alias.asname
            )
    return names


def _application_name_of(call):
    """Return the ``application_name`` literal of a call, or None.

    None whenever it is not a plain string literal, so such a call is never
    grouped with any other.
    """
    for keyword in call.keywords:
        if keyword.arg != "plugin_config" or not isinstance(keyword.value, ast.Call):
            continue
        for inner in keyword.value.keywords:
            if inner.arg == "application_name" and isinstance(
                inner.value, ast.Constant
            ):
                return inner.value.value
    return None


def _shared_plugin_call_hosts(tree):
    """Yield the application_name of each OLApisixSharedPlugins call.

    Counts attribute-style calls (``apisix.OLApisixSharedPlugins(...)``) and
    aliased imports as well as bare ones, so neither spelling slips past the
    check below.
    """
    local_names = _local_names_for(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Name) and func.id in local_names) or (
            isinstance(func, ast.Attribute) and func.attr == SHARED_PLUGINS_CLASS
        ):
            yield _application_name_of(node)


def test_no_application_hand_writes_two_shared_plugin_configs():
    """A host that needs a second shared plugin config has to go through
    ol_apisix_shared_plugins_variants, so the plugin list is shared by
    construction. Two configs built by hand for one application is the shape
    that drifted twice on api.learn, and the component tests above cannot see
    it because they only ever exercise one config at a time.

    Grouped by ``application_name`` across the whole tree rather than per file:
    the invariant is one plugin list per host, so two configs for two different
    applications are fine wherever they live (edxapp and meilisearch are that
    case today), and splitting one host's two configs into sibling modules is
    not a way out.
    """
    applications = Path(__file__).parents[4] / "src/ol_infrastructure/applications"
    modules = sorted(applications.rglob("*.py"))
    hosts: collections.Counter[str] = collections.Counter()
    unparsed = {}
    for module in modules:
        try:
            tree = ast.parse(module.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            unparsed[str(module.relative_to(applications))] = str(exc)
            continue
        hosts.update(
            host for host in _shared_plugin_call_hosts(tree) if host is not None
        )

    # Without these the test passes by scanning nothing -- a moved test file
    # (parents[4] no longer resolving) or a renamed class would leave it
    # permanently green, and it is the only guard behind the invariant.
    assert modules, f"scanned no application modules under {applications}"
    assert hosts, f"found no {SHARED_PLUGINS_CLASS} calls under {applications}"
    assert not unparsed, f"could not parse: {unparsed}"

    offenders = {host: count for host, count in hosts.items() if count > 1}
    assert not offenders, (
        "These applications build more than one "
        f"{SHARED_PLUGINS_CLASS} by hand, so their plugin lists have to be kept "
        f"in step by hand: {offenders}. Use ol_apisix_shared_plugins_variants "
        "instead."
    )
