"""Org IdPs carry routing arguments that Keycloak 26.8 accepts.

From 26.8 an email-domain redirect needs an org domain to hang off, and a
domain routes to one IdP, so the redirect follows the org's domains and an IdP
can be pinned to one of them.
"""

import asyncio
from typing import Any
from unittest import mock

import pulumi
import pulumi_keycloak as keycloak
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak import org_sso_helpers
from ol_infrastructure.substructure.keycloak.org_sso_helpers import (
    OIDCIdpConfig,
    OrgConfig,
    onboard_oidc_org,
)

REALM = "olapps"


class _RecordingMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return [f"{args.name}_id", dict(args.inputs)]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}


def _idp_inputs(org_domains: list[str], org_domain: str = "ANY") -> dict[str, Any]:
    recording = _RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    @pulumi.runtime.test
    def register():
        opts = pulumi.ResourceOptions()
        with mock.patch.object(
            org_sso_helpers,
            "oidc_identity_provider_args_from_discovery_url",
            return_value={
                "authorization_url": "https://idp.example.com/auth",
                "token_url": "https://idp.example.com/token",
            },
        ):
            onboard_oidc_org(
                OIDCIdpConfig(
                    idp_alias="partner",
                    idp_display_name="Partner",
                    org_oidc_metadata_url="https://idp.example.com/.well-known/openid-configuration",
                    first_login_flow=keycloak.authentication.Flow(
                        "first-login", realm_id=REALM, alias="first-login", opts=opts
                    ),
                    realm_id=REALM,
                    resource_options=opts,
                    client_id="client",
                    org_domain=org_domain,
                ),
                OrgConfig(
                    org_domains=org_domains,
                    org_name="Partner",
                    org_alias="Partner",
                    learn_domain="learn.example.com",
                    realm_id=REALM,
                    resource_options=opts,
                ),
            )

    register()
    return next(
        args.inputs
        for args in recording.resources
        if args.typ == "keycloak:oidc/identityProvider:IdentityProvider"
    )


@pytest.mark.parametrize(
    ("org_domains", "redirect"),
    [([], False), (["a.example.com", "b.example.com"], True)],
)
def test_redirect_follows_org_domains(org_domains, redirect):
    """The provider rejects the redirect on 26.8 when the org has no domains."""
    inputs = _idp_inputs(org_domains)
    assert inputs["orgRedirectModeEmailMatches"] is redirect
    assert inputs["orgDomain"] == "ANY"
    assert inputs["organizationId"] == "ol-apps-Partner-organization_id"


def test_idp_can_be_pinned_to_one_org_domain():
    """Two IdPs on one org cannot both claim every domain on 26.8."""
    inputs = _idp_inputs(
        ["a.example.com", "stu.a.example.com"], org_domain="stu.a.example.com"
    )
    assert inputs["orgDomain"] == "stu.a.example.com"
