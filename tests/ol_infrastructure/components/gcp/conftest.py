"""Fixtures for GCP component tests."""

import asyncio

import pulumi
import pytest


class GCPMocks(pulumi.runtime.Mocks):
    """Mock implementation for testing GCP components."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Mock resource creation."""
        outputs = dict(args.inputs)
        if args.typ == "gcp:serviceaccount/account:Account":
            account_id = args.inputs.get("accountId", args.name)
            project = args.inputs.get("project", "test-project")
            outputs["email"] = f"{account_id}@{project}.iam.gserviceaccount.com"
            outputs["name"] = f"projects/{project}/serviceAccounts/{outputs['email']}"
            outputs["uniqueId"] = "100000000000000000001"
        elif args.typ == "gcp:projects/apiKey:ApiKey":
            outputs["keyString"] = "test-key-string"
            outputs["uid"] = f"{args.name}-uid"
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        """Mock data source calls. No component here invokes a data source."""
        return {}


@pytest.fixture(autouse=True)
def gcp_mocks():
    """Set up GCPMocks for each test, preventing cross-test pollution."""
    # Python 3.14+ compatibility: set_mocks() requires a running event loop.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    pulumi.runtime.set_mocks(GCPMocks())
