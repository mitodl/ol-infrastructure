"""Scoping helpers for the data lake: Glue namespaces, the Deny, bucket ARNs."""

import pytest

from ol_infrastructure.lib.aws.iam_helper import (
    DATA_LAKE_STAGES,
    cross_environment_glue_denial,
    data_lake_bucket_arns,
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


def test_bucket_arns_cover_every_stage_and_exclude_the_landing_zone():
    arns = data_lake_bucket_arns("qa")
    buckets = [f"arn:aws:s3:::ol-data-lake-{stage}-qa" for stage in DATA_LAKE_STAGES]
    assert arns == buckets + [f"{bucket}/*" for bucket in buckets]
    assert not any("landing-zone" in arn for arn in arns)


def test_bucket_arns_name_no_other_environment():
    assert all(
        arn.removesuffix("/*").endswith("-qa") for arn in data_lake_bucket_arns("qa")
    )
