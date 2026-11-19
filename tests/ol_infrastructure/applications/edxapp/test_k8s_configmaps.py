"""Unit tests for k8s_configmaps module."""

import pytest

from ol_infrastructure.lib.pulumi_helper import StackInfo


@pytest.mark.parametrize(
    ("env_suffix", "expected"),
    [("production", True), ("qa", False), ("ci", False)],
)
def test_uses_cms_export_queue_only_in_production(env_suffix, expected):
    """The export queue settings and its worker exist only in production."""
    # Imported here: k8s_configmaps builds a boto client at import, which needs
    # the AWS env that the autouse set_aws_env_vars fixture sets after collection.
    from ol_infrastructure.applications.edxapp.k8s_configmaps import (  # noqa: PLC0415
        uses_cms_export_queue,
    )

    stack_info = StackInfo(
        name=env_suffix.capitalize(),
        namespace="ol-application-edxapp",
        env_prefix="mitxonline",
        env_suffix=env_suffix,
        full_name=f"organization/ol-application-edxapp/mitxonline.{env_suffix}",
        k8s_name=f"ol-application-edxapp.mitxonline.{env_suffix}",
        project_name="ol-application-edxapp",
    )

    assert uses_cms_export_queue(stack_info) is expected
