"""Pulumi mocks that record every registered resource."""

from typing import Any

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

    def of_type(self, typ: str) -> list[dict[str, Any]]:
        """Return the inputs of every recorded resource of Pulumi type ``typ``."""
        return [r.inputs for r in self.resources if r.typ == typ]
