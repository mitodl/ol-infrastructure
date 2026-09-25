"""readable_data_lake_environments must not name a lake the Glue Deny lists."""

import pytest

from ol_infrastructure.lib.aws.iam_helper import (
    cross_environment_glue_denial,
    data_lake_glue_namespaces,
    readable_data_lake_environments,
)


@pytest.mark.parametrize(
    ("env_suffix", "expected"),
    [
        ("production", ["qa", "production"]),
        ("qa", ["qa"]),
        ("ci", ["qa"]),
    ],
)
def test_readable_environments(env_suffix, expected):
    assert readable_data_lake_environments(env_suffix) == expected


@pytest.mark.parametrize("env_suffix", ["production", "qa", "ci"])
def test_no_readable_lake_is_denied(env_suffix):
    denied = {
        resource
        for statement in cross_environment_glue_denial(env_suffix)
        for resource in statement["Resource"]
    }
    for environment in readable_data_lake_environments(env_suffix):
        for namespace in data_lake_glue_namespaces(environment):
            assert f"arn:aws:glue:*:*:database/{namespace}" not in denied
