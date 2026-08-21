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
    oidc_gateway_pre_function_plugin,
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
