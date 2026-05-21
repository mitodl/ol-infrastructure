"""Tests for OLBucket component and S3BucketConfig versioning.

This test validates:
1. S3BucketConfig versioning_status field accepts valid values
2. S3BucketConfig versioning_status rejects invalid values
3. versioning_status takes precedence over versioning_enabled
4. OLBucket creates BucketVersioning resource with correct status
5. S3BucketConfig archive tiering fields accept and reject valid/invalid values
6. OLBucket creates/omits BucketIntelligentTieringConfiguration based on config
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


class TestS3BucketConfigArchiveTieringValidation:
    """Test S3BucketConfig validation for archive access tier fields."""

    @staticmethod
    def get_valid_tags():
        return {
            "OU": "operations",
            "Environment": "test",
            "Application": "test-app",
            "Owner": "test-owner",
        }

    # --- archive_access_days ---

    def test_archive_access_days_default_none(self):
        """Archive access tier is disabled by default."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            tags=self.get_valid_tags(),
        )
        assert config.intelligent_tiering_archive_access_days is None
        assert config.intelligent_tiering_deep_archive_access_days is None

    def test_archive_access_days_valid_minimum(self):
        """Minimum valid value (90) is accepted."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            intelligent_tiering_archive_access_days=90,
            tags=self.get_valid_tags(),
        )
        assert config.intelligent_tiering_archive_access_days == 90

    def test_archive_access_days_valid_maximum(self):
        """Maximum valid value (730) is accepted."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            intelligent_tiering_archive_access_days=730,
            tags=self.get_valid_tags(),
        )
        assert config.intelligent_tiering_archive_access_days == 730

    def test_archive_access_days_below_minimum(self):
        """Values below 90 days are rejected."""
        with pytest.raises(ValueError, match="must be >= 90"):
            S3BucketConfig(
                bucket_name="test-bucket",
                intelligent_tiering_archive_access_days=89,
                tags=self.get_valid_tags(),
            )

    def test_archive_access_days_above_maximum(self):
        """Values above 730 days are rejected."""
        with pytest.raises(ValueError, match="must be <= 730"):
            S3BucketConfig(
                bucket_name="test-bucket",
                intelligent_tiering_archive_access_days=731,
                tags=self.get_valid_tags(),
            )

    def test_archive_access_days_skipped_when_tiering_disabled(self):
        """Validator is skipped entirely when intelligent_tiering_enabled=False."""
        # Would normally be invalid (below minimum), but validation is
        # skipped because intelligent tiering is disabled, so these values
        # are irrelevant and should not raise.
        config = S3BucketConfig(
            bucket_name="test-bucket",
            intelligent_tiering_enabled=False,
            intelligent_tiering_archive_access_days=89,
            intelligent_tiering_deep_archive_access_days=179,
            tags=self.get_valid_tags(),
        )
        assert config.intelligent_tiering_archive_access_days == 89

    # --- deep_archive_access_days ---

    def test_deep_archive_access_days_valid_minimum(self):
        """Minimum valid value (180) is accepted."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            intelligent_tiering_archive_access_days=90,
            intelligent_tiering_deep_archive_access_days=180,
            tags=self.get_valid_tags(),
        )
        assert config.intelligent_tiering_deep_archive_access_days == 180

    def test_deep_archive_access_days_valid_maximum(self):
        """Maximum valid value (730) is accepted."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            intelligent_tiering_archive_access_days=90,
            intelligent_tiering_deep_archive_access_days=730,
            tags=self.get_valid_tags(),
        )
        assert config.intelligent_tiering_deep_archive_access_days == 730

    def test_deep_archive_access_days_below_minimum(self):
        """Values below 180 days are rejected."""
        with pytest.raises(ValueError, match="must be >= 180"):
            S3BucketConfig(
                bucket_name="test-bucket",
                intelligent_tiering_deep_archive_access_days=179,
                tags=self.get_valid_tags(),
            )

    def test_deep_archive_access_days_above_maximum(self):
        """Values above 730 days are rejected."""
        with pytest.raises(ValueError, match="must be <= 730"):
            S3BucketConfig(
                bucket_name="test-bucket",
                intelligent_tiering_deep_archive_access_days=731,
                tags=self.get_valid_tags(),
            )

    def test_deep_must_exceed_archive(self):
        """deep_archive days must be strictly greater than archive_access_days."""
        with pytest.raises(ValueError, match="must be greater than"):
            S3BucketConfig(
                bucket_name="test-bucket",
                intelligent_tiering_archive_access_days=180,
                intelligent_tiering_deep_archive_access_days=180,
                tags=self.get_valid_tags(),
            )

    def test_deep_without_archive_is_valid(self):
        """deep_archive_access_days can be set without archive_access_days."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            intelligent_tiering_archive_access_days=None,
            intelligent_tiering_deep_archive_access_days=180,
            tags=self.get_valid_tags(),
        )
        assert config.intelligent_tiering_deep_archive_access_days == 180


class TestOLBucketArchiveTieringResource:
    """Test OLBucket creates/omits BucketIntelligentTieringConfiguration."""

    @staticmethod
    def get_valid_tags():
        return {
            "OU": "operations",
            "Environment": "test",
            "Application": "test-app",
            "Owner": "test-owner",
        }

    @pulumi.runtime.test
    def test_no_archive_tiering_by_default(self):
        """OLBucket does not create BucketIntelligentTieringConfiguration by default."""
        config = S3BucketConfig(
            bucket_name="test-default-bucket",
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-default-tiering", config=config)

        def check_no_tiering_config(tiering_resource):
            assert tiering_resource is None, (
                "BucketIntelligentTieringConfiguration should not be created "
                "when archive tier fields are None (default)"
            )

        return pulumi.Output.from_input(bucket.bucket_intelligent_tiering).apply(
            check_no_tiering_config
        )

    @pulumi.runtime.test
    def test_archive_tiering_created_when_opted_in(self):
        """OLBucket creates BucketIntelligentTieringConfiguration when opted in."""
        config = S3BucketConfig(
            bucket_name="test-archive-bucket",
            intelligent_tiering_archive_access_days=90,
            intelligent_tiering_deep_archive_access_days=180,
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-archive-tiering", config=config)

        def check_tiering_created(tiering_resource):
            assert tiering_resource is not None, (
                "BucketIntelligentTieringConfiguration should be created "
                "when archive tier days are set"
            )

        return pulumi.Output.from_input(bucket.bucket_intelligent_tiering).apply(
            check_tiering_created
        )

    @pulumi.runtime.test
    def test_archive_only_tiering(self):
        """OLBucket creates config with only ARCHIVE_ACCESS when deep is None."""
        config = S3BucketConfig(
            bucket_name="test-archive-only-bucket",
            intelligent_tiering_archive_access_days=90,
            intelligent_tiering_deep_archive_access_days=None,
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-archive-only", config=config)

        def check_archive_only(tiering_resource):
            assert tiering_resource is not None, (
                "BucketIntelligentTieringConfiguration should be created "
                "when archive_access_days is set"
            )

        return pulumi.Output.from_input(bucket.bucket_intelligent_tiering).apply(
            check_archive_only
        )

    @pulumi.runtime.test
    def test_no_tiering_config_when_tiering_disabled(self):
        """OLBucket omits BucketIntelligentTieringConfiguration when disabled."""
        config = S3BucketConfig(
            bucket_name="test-no-tiering-bucket",
            intelligent_tiering_enabled=False,
            intelligent_tiering_archive_access_days=90,
            intelligent_tiering_deep_archive_access_days=180,
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-tiering-disabled", config=config)

        def check_no_config(tiering_resource):
            assert tiering_resource is None, (
                "BucketIntelligentTieringConfiguration should not be created "
                "when intelligent_tiering_enabled=False"
            )

        return pulumi.Output.from_input(bucket.bucket_intelligent_tiering).apply(
            check_no_config
        )


class TestS3BucketConfigAbortMPUValidation:
    """Test S3BucketConfig validation for abort_incomplete_mpu_days."""

    @staticmethod
    def get_valid_tags():
        return {
            "OU": "operations",
            "Environment": "test",
            "Application": "test-app",
            "Owner": "test-owner",
        }

    def test_abort_mpu_default_is_seven(self):
        """abort_incomplete_mpu_days defaults to 7."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            tags=self.get_valid_tags(),
        )
        assert config.abort_incomplete_mpu_days == 7

    def test_abort_mpu_can_be_disabled(self):
        """abort_incomplete_mpu_days=None disables the rule."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            abort_incomplete_mpu_days=None,
            tags=self.get_valid_tags(),
        )
        assert config.abort_incomplete_mpu_days is None

    def test_abort_mpu_valid_custom_value(self):
        """Any positive integer is accepted."""
        config = S3BucketConfig(
            bucket_name="test-bucket",
            abort_incomplete_mpu_days=30,
            tags=self.get_valid_tags(),
        )
        assert config.abort_incomplete_mpu_days == 30

    def test_abort_mpu_zero_rejected(self):
        """Zero is rejected."""
        with pytest.raises(ValueError, match="must be >= 1"):
            S3BucketConfig(
                bucket_name="test-bucket",
                abort_incomplete_mpu_days=0,
                tags=self.get_valid_tags(),
            )

    def test_abort_mpu_negative_rejected(self):
        """Negative values are rejected."""
        with pytest.raises(ValueError, match="must be >= 1"):
            S3BucketConfig(
                bucket_name="test-bucket",
                abort_incomplete_mpu_days=-1,
                tags=self.get_valid_tags(),
            )


class TestOLBucketAbortMPUResource:
    """Test OLBucket creates/omits AbortIncompleteMultipartUpload lifecycle rule."""

    @staticmethod
    def get_valid_tags():
        return {
            "OU": "operations",
            "Environment": "test",
            "Application": "test-app",
            "Owner": "test-owner",
        }

    @pulumi.runtime.test
    def test_abort_mpu_rule_present_by_default(self):
        """OLBucket lifecycle config includes abort-MPU rule by default."""
        config = S3BucketConfig(
            bucket_name="test-abort-default",
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-abort-default", config=config)

        def check_abort_rule(args):
            lifecycle, rules = args
            assert lifecycle is not None, "BucketLifecycleConfiguration should exist"
            rule_ids = [r.get("id") for r in (rules or [])]
            assert "abort-incomplete-multipart-uploads" in rule_ids, (
                f"Expected abort rule in lifecycle rules, got: {rule_ids}"
            )

        return pulumi.Output.all(
            bucket.bucket_lifecycle,
            bucket.bucket_lifecycle.rules.apply(
                lambda r: [
                    {
                        "id": rule.get("id")
                        if isinstance(rule, dict)
                        else getattr(rule, "id", None)
                    }
                    for rule in (r or [])
                ]
            ),
        ).apply(check_abort_rule)

    @pulumi.runtime.test
    def test_abort_mpu_rule_absent_when_disabled(self):
        """OLBucket omits abort-MPU rule when abort_incomplete_mpu_days=None."""
        config = S3BucketConfig(
            bucket_name="test-abort-disabled",
            intelligent_tiering_enabled=False,
            abort_incomplete_mpu_days=None,
            tags=self.get_valid_tags(),
        )
        bucket = OLBucket("test-abort-disabled", config=config)

        def check_no_lifecycle(lifecycle):
            assert lifecycle is None, (
                "BucketLifecycleConfiguration should not exist when both "
                "intelligent tiering and abort MPU are disabled"
            )

        return pulumi.Output.from_input(bucket.bucket_lifecycle).apply(
            check_no_lifecycle
        )
