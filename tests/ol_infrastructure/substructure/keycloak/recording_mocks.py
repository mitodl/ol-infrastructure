"""Pulumi mocks that record every registered resource for the Keycloak tests."""

import pulumi


class RecordingMocks(pulumi.runtime.Mocks):
    """Return each resource's inputs as its outputs and keep its registration args."""

    def __init__(self) -> None:
        self.resources: list[pulumi.runtime.MockResourceArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        return [f"{args.name}_id", dict(args.inputs)]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        return {}
