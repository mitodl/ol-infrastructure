"""The contact-us launcher is enabled per environment, not wherever it could be.

The launcher replaces Appzi on the mitxonline MFEs.  Appzi runs on production
only; the launcher is wanted on rc too, so it can be exercised against the rc
learn-ai backend before it reaches learners.  Everything else stays dark.

The endpoint is a field rather than something derived from ``mit_learn_api_url``
because that URL is also set on mitxonline CI, and deriving would have switched
CI on by accident -- against a learn-ai deployment nobody has pointed a support
bot at.
"""

import pytest

from bridge.settings.openedx.accessors import fetch_applications_by_type
from ol_concourse.pipelines.open_edx.mfe.pipeline import OpenEdxVars, mfe_params
from ol_concourse.pipelines.open_edx.mfe.values import deployments

ENABLED = {
    "mitxonline-qa": "https://api.rc.learn.mit.edu/ai/http/support_agent/",
    "mitxonline-production": "https://api.learn.mit.edu/ai/http/support_agent/",
}

ALL_ENV_VARS = [
    pytest.param(edx_var, deployment_name, id=edx_var.environment)
    for deployment_name, edx_vars in deployments.items()
    for edx_var in edx_vars
]


def _params(edx_var: OpenEdxVars, deployment_name: str) -> list[dict[str, str | None]]:
    mfes = fetch_applications_by_type(edx_var.release_name, deployment_name, "MFE")
    return [mfe_params(edx_var, mfe) for mfe in mfes]


@pytest.mark.parametrize(("edx_var", "deployment_name"), ALL_ENV_VARS)
def test_support_endpoint_matches_the_enabled_list(
    edx_var: OpenEdxVars, deployment_name: str
) -> None:
    """Only rc and production get an endpoint, and each gets its own backend."""
    expected = ENABLED.get(edx_var.environment)
    for params in _params(edx_var, deployment_name):
        assert params["CONTACT_US_SUPPORT_API_URL"] == expected


@pytest.mark.parametrize(("edx_var", "deployment_name"), ALL_ENV_VARS)
def test_every_mfe_in_an_enabled_environment_gets_it(
    edx_var: OpenEdxVars, deployment_name: str
) -> None:
    """Not just the learning MFE -- the launcher is footer-wide, like Appzi."""
    if edx_var.environment not in ENABLED:
        pytest.skip(f"{edx_var.environment} is intentionally dark")
    all_params = _params(edx_var, deployment_name)
    assert len(all_params) > 1
    assert all(params["CONTACT_US_SUPPORT_API_URL"] for params in all_params)


def test_appzi_is_still_production_only() -> None:
    """The launcher ships beside Appzi; removing Appzi is a separate step."""
    appzi_envs = {
        edx_var.environment
        for edx_vars in deployments.values()
        for edx_var in edx_vars
        if edx_var.appzi_url
    }
    assert appzi_envs == {"mitxonline-production"}
