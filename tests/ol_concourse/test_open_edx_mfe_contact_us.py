"""The contact-us launcher is enabled per environment, not wherever it could be.

The launcher replaces Appzi on the mitxonline MFEs.  Appzi runs on production
only; the launcher is wanted on rc too, so it can be exercised against the rc
learn-ai backend before it reaches learners.  Everything else stays dark.
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


def _params_by_mfe(
    edx_var: OpenEdxVars, deployment_name: str
) -> dict[str, dict[str, str | None]]:
    mfes = fetch_applications_by_type(edx_var.release_name, deployment_name, "MFE")
    return {mfe.application: mfe_params(edx_var, mfe) for mfe in mfes}


@pytest.mark.parametrize(("edx_var", "deployment_name"), ALL_ENV_VARS)
def test_support_endpoint_matches_the_enabled_list(
    edx_var: OpenEdxVars, deployment_name: str
) -> None:
    """Only rc and production get an endpoint, and each gets its own backend."""
    expected = ENABLED.get(edx_var.environment)
    for params in _params_by_mfe(edx_var, deployment_name).values():
        assert params["CONTACT_US_SUPPORT_API_URL"] == expected


@pytest.mark.parametrize(("edx_var", "deployment_name"), ALL_ENV_VARS)
def test_every_mfe_in_an_enabled_environment_gets_it(
    edx_var: OpenEdxVars, deployment_name: str
) -> None:
    """Not just the learning MFE -- the launcher is footer-wide, like Appzi."""
    if edx_var.environment not in ENABLED:
        pytest.skip(f"{edx_var.environment} is intentionally dark")
    by_mfe = _params_by_mfe(edx_var, deployment_name)
    missing = [
        mfe
        for mfe, params in by_mfe.items()
        if not params["CONTACT_US_SUPPORT_API_URL"]
    ]
    assert not missing
