"""The scim-for-keycloak remote provider dynamic provider."""

from ol_infrastructure.providers.keycloak_scim.remote_provider import (
    ScimRemoteProviderSpec,
    _declared,
)


def test_declared_restores_ints_floated_by_the_engine() -> None:
    spec = ScimRemoteProviderSpec(
        realm="olapps",
        name="ol-mitxonline-scim",
        base_url="https://api.mitxonline.mit.edu/scim/v2",
        enabled=True,
        retry_interval_in_seconds=300,
        retry_max_attempts=5,
    )
    # The engine hands dynamic providers every number as a double.
    props = {
        "spec": {
            **spec.to_scim(),
            "retryIntervalInSeconds": 300.0,
            "retryMaxAttempts": 5.0,
        },
        "authentication": None,
    }

    declared = _declared(props)

    assert type(declared["retryIntervalInSeconds"]) is int
    assert declared["retryIntervalInSeconds"] == 300
    assert type(declared["retryMaxAttempts"]) is int
    assert declared["enabled"] is True
