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
from ol_infrastructure.components.services.apisix import (  # noqa: E402
    OLApisixOIDCConfig,
    OLApisixOIDCResources,
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
