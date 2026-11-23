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


def test_undeclared_capabilities_stay_out_of_the_request() -> None:
    """An attribute left unset is never sent, so it keeps its live value.

    The provider writes read-modify-write, so an attribute absent from the body
    is one Pulumi does not manage. Deployed stacks that declare no capabilities
    rely on this: the plugin defaults every flag off, and sending a default
    would turn off whatever the SCIM admin console has set.
    """
    spec = ScimRemoteProviderSpec(
        realm="olapps",
        name="ol-learn-scim",
        base_url="https://api.learn.mit.edu/scim/v2",
        enabled=True,
    )

    body = spec.to_scim()

    for attribute in (
        "filterSupported",
        "filterMaxResults",
        "sortSupported",
        "patchSupported",
        "changePasswordSupported",
        "bulkSupported",
        "bulkMaxOperations",
        "bulkMaxPayloadSize",
        "etagSupported",
    ):
        assert attribute not in body


def test_declared_capabilities_use_the_plugin_attribute_names() -> None:
    spec = ScimRemoteProviderSpec(
        realm="olapps",
        name="ol-learn-scim",
        base_url="https://api.learn.mit.edu/scim/v2",
        enabled=True,
        filter_supported=True,
        filter_max_results=50,
        sort_supported=True,
        patch_supported=True,
        bulk_supported=True,
        bulk_max_operations=100,
        bulk_max_payload_size=1048576,
    )

    body = spec.to_scim()

    assert body["filterSupported"] is True
    assert body["filterMaxResults"] == 50
    assert body["sortSupported"] is True
    assert body["patchSupported"] is True
    assert body["bulkSupported"] is True
    assert body["bulkMaxOperations"] == 100
    assert body["bulkMaxPayloadSize"] == 1048576
    # Still unmanaged, so still absent.
    assert "etagSupported" not in body
    assert "changePasswordSupported" not in body


def test_declared_restores_ints_in_the_capability_counts() -> None:
    """The counts cross the engine as doubles too, and the plugin rejects floats."""
    spec = ScimRemoteProviderSpec(
        realm="olapps",
        name="ol-learn-scim",
        base_url="https://api.learn.mit.edu/scim/v2",
        enabled=True,
        filter_max_results=50,
        bulk_max_operations=100,
        bulk_max_payload_size=1048576,
    )
    props = {
        "spec": {
            **spec.to_scim(),
            "filterMaxResults": 50.0,
            "bulkMaxOperations": 100.0,
            "bulkMaxPayloadSize": 1048576.0,
        },
        "authentication": None,
    }

    declared = _declared(props)

    assert type(declared["filterMaxResults"]) is int
    assert type(declared["bulkMaxOperations"]) is int
    assert type(declared["bulkMaxPayloadSize"]) is int
    assert declared["bulkMaxPayloadSize"] == 1048576
