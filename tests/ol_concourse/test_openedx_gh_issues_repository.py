"""Every Open edX deployment needs a release-gate repository.

The pipelines index OPENEDX_GH_ISSUES_REPOSITORY by deployment name, so a
deployment added to OpenEdxDeploymentName without an entry here only fails as a
KeyError when Concourse renders its pipeline.
"""

from typing import get_args

from bridge.settings.openedx.types import OpenEdxDeploymentName
from ol_concourse.pipelines.constants import OPENEDX_GH_ISSUES_REPOSITORY


def test_every_deployment_has_a_gate_repository() -> None:
    assert set(OPENEDX_GH_ISSUES_REPOSITORY) == set(get_args(OpenEdxDeploymentName))
