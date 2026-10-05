"""Tests for the SFTPServer component's bucket lifecycle opt-in.

The default keeps every noncurrent version of a partner upload, so the tests
cover both sides: unset creates no lifecycle configuration, and a set value
creates the multipart abort and the noncurrent expiry with that many days.
"""

import asyncio

import pulumi
import pytest
from pydantic import ValidationError

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.components.aws.sftp import SFTPServer, SFTPServerConfig

NONCURRENT_DAYS = 90
VALID_TAGS = {
    "OU": "operations",
    "Environment": "test",
    "Application": "test-app",
    "Owner": "test-owner",
}


class SFTPServerMocks(pulumi.runtime.Mocks):
    """Echo resource inputs and answer the caller identity lookup."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Mock resource creation."""
        return [f"{args.name}_id", args.inputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        """Mock data source calls."""
        return {"accountId": "123456789012"}


@pytest.fixture(autouse=True)
def sftp_server_mocks():
    """Replace the package-level mocks, which do not answer getCallerIdentity."""
    pulumi.runtime.set_mocks(SFTPServerMocks())


def test_noncurrent_version_expiration_days_defaults_to_unset():
    """The opt-in is off unless a stack sets it."""
    config = SFTPServerConfig(
        server_name="test-sftp", bucket_name="test-sftp-bucket", tags=VALID_TAGS
    )

    assert config.noncurrent_version_expiration_days is None


def test_noncurrent_version_expiration_days_rejects_zero():
    """Zero days is not a valid S3 noncurrent expiry."""
    with pytest.raises(ValidationError):
        SFTPServerConfig(
            server_name="test-sftp",
            bucket_name="test-sftp-bucket",
            noncurrent_version_expiration_days=0,
            tags=VALID_TAGS,
        )


@pulumi.runtime.test
def test_no_lifecycle_configuration_by_default():
    """An SFTPServer that does not opt in keeps every version."""
    server = SFTPServer(
        SFTPServerConfig(
            server_name="test-sftp-default",
            bucket_name="test-sftp-default-bucket",
            tags=VALID_TAGS,
        )
    )

    def check(_):
        assert server.bucket_lifecycle is None

    return server.bucket.id.apply(check)


@pulumi.runtime.test
def test_lifecycle_configuration_created_when_opted_in():
    """Opting in creates both rules, with the configured noncurrent days."""
    server = SFTPServer(
        SFTPServerConfig(
            server_name="test-sftp-lifecycle",
            bucket_name="test-sftp-lifecycle-bucket",
            noncurrent_version_expiration_days=NONCURRENT_DAYS,
            tags=VALID_TAGS,
        )
    )
    assert server.bucket_lifecycle is not None

    def check(rules):
        by_id = {rule["id"]: rule for rule in rules}
        assert set(by_id) == {
            "abort-incomplete-multipart-uploads",
            "expire-noncurrent-versions",
        }
        assert all(rule["status"] == "Enabled" for rule in rules)
        expire = by_id["expire-noncurrent-versions"]
        assert (
            expire["noncurrent_version_expiration"]["noncurrent_days"]
            == NONCURRENT_DAYS
        )
        assert expire["expiration"]["expired_object_delete_marker"] is True
        abort = by_id["abort-incomplete-multipart-uploads"]
        assert abort["abort_incomplete_multipart_upload"]["days_after_initiation"] == 7

    return server.bucket_lifecycle.rules.apply(check)
