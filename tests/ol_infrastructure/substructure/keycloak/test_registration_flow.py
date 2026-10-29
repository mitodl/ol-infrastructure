"""The olapps registration flow must require reCAPTCHA with the config Keycloak reads.

Keycloak 26 reads the secret from ``secret.key`` (``secret`` is the pre-26 name it
migrates on first use). With an empty site key or secret the page renders no widget
and every registration fails the reCAPTCHA check.
The realm's flow binding points at the flow alias, so a changed name replaces the
bound flow.
"""

import asyncio

import pulumi
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.substructure.keycloak.registration_flow import (
    create_recaptcha_registration_flow,
)
from tests.ol_infrastructure.recording_mocks import RecordingMocks

PREFIX = "ol-registration"


@pytest.fixture
def resources() -> dict[str, pulumi.runtime.MockResourceArgs]:
    """Register the flow and return its resources by Pulumi name."""
    recording = RecordingMocks()
    pulumi.runtime.set_mocks(recording, preview=False)

    # pulumi.runtime.test waits for every resource registration to finish
    # before returning, so the mocks hold all of them once this returns.
    @pulumi.runtime.test
    def register():
        create_recaptcha_registration_flow(
            "olapps",
            PREFIX,
            recaptcha_site_key="site",
            recaptcha_secret_key="secret",  # pragma: allowlist secret
        )

    register()
    return {args.name: args for args in recording.resources}


def test_resource_names_and_aliases_match_the_deployed_flow(resources):
    """A renamed resource or alias replaces the flow the realm is bound to."""
    assert set(resources) == {
        f"{PREFIX}-flow",
        f"{PREFIX}-form",
        f"{PREFIX}-user-creation",
        f"{PREFIX}-password-validation",
        f"{PREFIX}-recaptcha",
        f"{PREFIX}-recaptcha-config",
    }
    assert resources[f"{PREFIX}-flow"].inputs["alias"] == f"{PREFIX}-flow"


def test_form_steps_match_the_builtin_registration_flow(resources):
    """Same steps and priorities as Keycloak's built-in flow, with reCAPTCHA on."""
    form = resources[f"{PREFIX}-form"].inputs
    assert (form["providerId"], form["authenticator"]) == (
        "form-flow",
        "registration-page-form",
    )
    steps = sorted(
        (
            args.inputs["priority"],
            args.inputs["authenticator"],
            args.inputs["requirement"],
        )
        for args in resources.values()
        if args.inputs.get("parentFlowAlias") == f"{PREFIX}-form"
    )
    assert steps == [
        (20, "registration-user-creation", "REQUIRED"),
        (50, "registration-password-action", "REQUIRED"),
        (60, "registration-recaptcha-action", "REQUIRED"),
    ]


def test_recaptcha_config_uses_keycloak_26_keys(resources):
    """``secret`` instead of ``secret.key`` would leave Keycloak 26 without one."""
    config = resources[f"{PREFIX}-recaptcha-config"].inputs["config"]
    assert config == {
        "site.key": "site",
        "secret.key": "secret",
        "action": "register",
        "recaptcha.v3": "false",
        "useRecaptchaNet": "true",
    }
