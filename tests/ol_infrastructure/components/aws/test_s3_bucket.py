"""Tests for OLBucket component and S3BucketConfig versioning.

This test validates:
1. S3BucketConfig versioning_status field accepts valid values
2. S3BucketConfig versioning_status rejects invalid values
3. versioning_status takes precedence over versioning_enabled
4. OLBucket creates BucketVersioning resource with correct status
"""

import asyncio
import os

# Set AWS environment variables before importing boto3-dependent modules
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = (
    "testing"  # pragma: allowlist secret  # noqa: S105
)

import pulumi
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig


class TestS3BucketConfigValidation:
    """Test S3BucketConfig validation for versioning_status field."""

    @staticmethod
    def get_valid_tags():
        """Return valid tags for testing."""
        return {
            "OU": "operations",
            "Environment": "test",
            "Application": "test-app",
            "Owner": "test-owner",
        }

    def test_versioning_status_enabled(self):
        """Test versioning_status='Enabled' is accepted."""
        config = S3BucketConfig(
            bucket_name="test-bucket-enabled",
            versioning_status="Enabled",
            tags=self.get_valid_tags(),
        )
        assert config.versioning_status == "Enabled"

    def test_versioning_status_suspended(self):
        """Test versioning_status='Suspended' is accepted."""
        config = S3BucketConfig(
            bucket_name="test-bucket-suspended",
            versioning_status="Suspended",
            tags=self.get_valid_tags(),
        )
        assert config.versioning_status == "Suspended"

    def test_versioning_status_disabled(self):
        """Test versioning_status='Disabled' is accepted."""
        config = S3BucketConfig(
            bucket_name="test-bucket-disabled",
            versioning_status="Disabled",
            tags=self.get_valid_tags(),
        )
        assert config.versioning_status == "Disabled"

    def test_versioning_status_invalid(self):
        """Test invalid versioning_status is rejected."""
        with pytest.raises(ValueError, match="versioning_status must be"):
            S3BucketConfig(
                bucket_name="test-bucket-invalid",
                versioning_status="InvalidStatus",
                tags=self.get_valid_tags(),
            )

    def test_versioning_status_none(self):
        """Test versioning_status defaults to None."""
        config = S3BucketConfig(
            bucket_name="test-bucket-none",
            tags=self.get_valid_tags(),
        )
        assert config.versioning_status is None

    def test_versioning_status_takes_precedence(self):
        """Test versioning_status takes precedence over versioning_enabled."""
        # When both are set, versioning_status should take precedence
        config = S3BucketConfig(
            bucket_name="test-bucket-precedence",
            versioning_status="Suspended",
            versioning_enabled=True,
            tags=self.get_valid_tags(),
        )
        assert config.versioning_status == "Suspended"
        assert config.versioning_enabled is True


@pytest.fixture
def pulumi_mocks():
    """Set up Pulumi mocks for testing."""

    class TestMocks(pulumi.runtime.Mocks):
        def new_resource(self, args):
            return [args.name + "_id", args.inputs]

        def call(self, args):  # noqa: ARG002
            return []

    # Set mocks before any resources are created
    pulumi.runtime.set_mocks(TestMocks())
    # Cleanup happens automatically after test


class TestOLBucketVersioningResource:
    """Test OLBucket component creates BucketVersioning resources correctly."""

    @staticmethod
    def get_valid_tags():
        """Return valid tags for testing."""
        return {
            "OU": "operations",
            "Environment": "test",
            "Application": "test-app",
            "Owner": "test-owner",
        }

    @pulumi.runtime.test
    def test_bucket_with_suspended_versioning(self):
        """Test OLBucket creates BucketVersioning with Suspended status."""
        config = S3BucketConfig(
            bucket_name="test-bucket-suspended",
            versioning_status="Suspended",
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-suspended", config=config)

        def check_suspended_versioning(args):
            bucket_versioning, versioning_status = args
            assert bucket_versioning is not None, (
                "BucketVersioning resource should be created for Suspended status"
            )
            assert versioning_status == "Suspended", (
                f"Versioning status should be 'Suspended', got '{versioning_status}'"
            )

        return pulumi.Output.all(
            bucket.bucket_versioning,
            bucket.bucket_versioning.versioning_configuration.apply(
                lambda vc: vc.get("status") if vc else None
            ),
        ).apply(check_suspended_versioning)

    @pulumi.runtime.test
    def test_bucket_with_enabled_versioning(self):
        """Test OLBucket creates BucketVersioning with Enabled status."""
        config = S3BucketConfig(
            bucket_name="test-bucket-enabled",
            versioning_status="Enabled",
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-enabled", config=config)

        def check_enabled_versioning(args):
            bucket_versioning, versioning_status = args
            assert bucket_versioning is not None, (
                "BucketVersioning resource should be created for Enabled status"
            )
            assert versioning_status == "Enabled", (
                f"Versioning status should be 'Enabled', got '{versioning_status}'"
            )

        return pulumi.Output.all(
            bucket.bucket_versioning,
            bucket.bucket_versioning.versioning_configuration.apply(
                lambda vc: vc.get("status") if vc else None
            ),
        ).apply(check_enabled_versioning)

    @pulumi.runtime.test
    def test_bucket_without_versioning_status(self):
        """Test OLBucket with versioning_enabled=False and no versioning_status."""
        config = S3BucketConfig(
            bucket_name="test-bucket-no-versioning",
            versioning_enabled=False,
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-no-versioning", config=config)

        def check_no_versioning(args):
            bucket_versioning = args
            assert bucket_versioning is None, (
                "BucketVersioning resource should not be created when "
                "versioning_enabled=False and versioning_status=None"
            )

        return pulumi.Output.from_input(bucket.bucket_versioning).apply(
            check_no_versioning
        )

    @pulumi.runtime.test
    def test_bucket_with_versioning_enabled_true(self):
        """Test OLBucket creates BucketVersioning with Enabled when enabled."""
        config = S3BucketConfig(
            bucket_name="test-bucket-versioning-enabled",
            versioning_enabled=True,
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-versioning-enabled", config=config)

        def check_enabled_by_flag(args):
            bucket_versioning, versioning_status = args
            assert bucket_versioning is not None, (
                "BucketVersioning resource should be created when "
                "versioning_enabled=True"
            )
            assert versioning_status == "Enabled", (
                f"Versioning status should be 'Enabled', got '{versioning_status}'"
            )

        return pulumi.Output.all(
            bucket.bucket_versioning,
            bucket.bucket_versioning.versioning_configuration.apply(
                lambda vc: vc.get("status") if vc else None
            ),
        ).apply(check_enabled_by_flag)

    @pulumi.runtime.test
    def test_bucket_versioning_status_precedence_over_flag(self):
        """Test versioning_status='Enabled' overrides versioning_enabled=False."""
        config = S3BucketConfig(
            bucket_name="test-bucket-precedence",
            versioning_status="Enabled",
            versioning_enabled=False,
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-precedence", config=config)

        def check_status_precedence(args):
            bucket_versioning, versioning_status = args
            assert bucket_versioning is not None, (
                "BucketVersioning resource should be created "
                "when versioning_status is set"
            )
            assert versioning_status == "Enabled", (
                f"versioning_status should take precedence, got '{versioning_status}'"
            )

        return pulumi.Output.all(
            bucket.bucket_versioning,
            bucket.bucket_versioning.versioning_configuration.apply(
                lambda vc: vc.get("status") if vc else None
            ),
        ).apply(check_status_precedence)
