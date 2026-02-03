"""AWS S3 bucket component for standardized bucket creation and management.

This module provides a reusable Pulumi component resource for creating S3 buckets
with common configurations including versioning, encryption, logging, lifecycle rules,
and intelligent tiering for cost optimization.
"""

import pulumi
from pulumi import Output
from pulumi_aws import s3
from pydantic import ConfigDict, Field, model_validator

from ol_infrastructure.lib.ol_types import AWSBase


class S3BucketConfig(AWSBase):
    """Configuration for an S3 bucket component."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    bucket_name: str | None = Field(
        default=None,
        description=(
            "The name of the S3 bucket. If not provided, Pulumi will generate one. "
            "Note: If a name is provided, it must be globally unique."
        ),
    )
    versioning_enabled: bool = Field(
        default=False, description="Whether versioning is enabled for the bucket."
    )
    acl: str | None = Field(
        default=None,
        description=(
            "The canned ACL to apply to the bucket. Conflicts with "
            "`block_public_acls`, `block_public_policy`, `ignore_public_acls`, "
            "`restrict_public_buckets`. ACLs are generally discouraged in favor "
            "of bucket policies and public access blocks."
        ),
    )
    force_destroy: bool = Field(
        default=False,
        description=(
            "A boolean that indicates all objects (including locked objects) "
            "should be deleted from the bucket when the bucket is destroyed. "
            "These objects are not recoverable. This only affects destroy "
            "operations."
        ),
    )
    block_public_acls: bool = Field(
        default=True,
        description=(
            "Whether Amazon S3 should block public access control lists (ACLs) "
            "for this bucket."
        ),
    )
    block_public_policy: bool = Field(
        default=True,
        description=(
            "Whether Amazon S3 should block public bucket policies for this bucket."
        ),
    )
    ignore_public_acls: bool = Field(
        default=True,
        description="Whether Amazon S3 should ignore public ACLs for this bucket.",
    )
    restrict_public_buckets: bool = Field(
        default=True,
        description=(
            "Whether Amazon S3 should restrict public bucket policies for this bucket."
        ),
    )
    logging_target_bucket: str | None = Field(
        default=None,
        description="The name of the bucket where access logs will be stored.",
    )
    logging_target_prefix: str | None = Field(
        default=None,
        description="The prefix for the access log objects.",
    )
    lifecycle_rules: list[s3.BucketLifecycleConfigurationRuleArgs] | None = Field(
        default=None,
        description="A list of lifecycle rules for the bucket.",
    )
    website_configuration: s3.BucketWebsiteConfigurationArgs | None = Field(
        default=None,
        description="Website configuration for the bucket.",
    )
    server_side_encryption_enabled: bool = Field(
        default=False,
        description="Whether server-side encryption is enabled for the bucket.",
    )
    sse_algorithm: str = Field(
        default="aws:kms",
        description=(
            "The server-side encryption algorithm to use. "
            "Valid values: 'AES256' (SSE-S3) or 'aws:kms' (SSE-KMS). "
            "Default is 'aws:kms'."
        ),
    )
    kms_key_id: str | Output[str] | None = Field(
        default=None,
        description=(
            "The KMS key ID to use for server-side encryption. "
            "Can be a string or Pulumi Output[str]. "
            "Only used when sse_algorithm is 'aws:kms'. "
            "If not provided with aws:kms, uses AWS-managed KMS key."
        ),
    )
    bucket_key_enabled: bool | None = Field(
        default=None,
        description="Whether S3 Bucket Keys are enabled for the bucket.",
    )
    intelligent_tiering_enabled: bool = Field(
        default=True,
        description=(
            "Whether to enable S3 Intelligent-Tiering for cost optimization. "
            "Objects will automatically move between access tiers based on "
            "usage patterns."
        ),
    )
    intelligent_tiering_days: int = Field(
        default=90,
        description=(
            "Number of days after which objects transition to Intelligent-Tiering. "
            "Only used if intelligent_tiering_enabled is True."
        ),
    )
    cors_rules: list[s3.BucketCorsConfigurationCorsRuleArgs] | None = Field(
        default=None,
        description="CORS configuration rules for the bucket.",
    )
    ownership_controls: str = Field(
        default="BucketOwnerEnforced",
        description=(
            "Object ownership setting for the bucket. Valid values: "
            "BucketOwnerPreferred, ObjectWriter, BucketOwnerEnforced."
        ),
    )
    bucket_policy_document: str | None = Field(
        default=None,
        description="The bucket policy document as a JSON string.",
    )

    @model_validator(mode="after")
    def check_acl_and_public_access_blocks(self) -> "S3BucketConfig":
        """Validate that ACL is not used with public access blocks set to False."""
        if self.acl and (
            not self.block_public_acls
            or not self.block_public_policy
            or not self.ignore_public_acls
            or not self.restrict_public_buckets
        ):
            error_message = (
                "Cannot specify `acl` when any public access block setting is False. "
                "ACLs are generally discouraged in favor of bucket policies and public "
                "access blocks."
            )
            raise ValueError(error_message)
        return self

    @model_validator(mode="after")
    def check_intelligent_tiering_days(self) -> "S3BucketConfig":
        """Validate intelligent tiering days configuration."""
        if self.intelligent_tiering_enabled and self.intelligent_tiering_days < 0:
            error_message = (
                "intelligent_tiering_days must be a non-negative integer when "
                "intelligent_tiering_enabled is True."
            )
            raise ValueError(error_message)
        return self

    @model_validator(mode="after")
    def check_encryption_config(self) -> "S3BucketConfig":
        """Validate encryption configuration.

        Note: When server_side_encryption_enabled is True but kms_key_id is None,
        AWS uses its default managed key for encryption (valid configuration).
        """
        if self.sse_algorithm not in ["AES256", "aws:kms"]:
            error_message = (
                "sse_algorithm must be either 'AES256' (SSE-S3) or 'aws:kms' (SSE-KMS)"
            )
            raise ValueError(error_message)
        if self.sse_algorithm == "AES256" and self.kms_key_id:
            error_message = (
                "kms_key_id cannot be specified when sse_algorithm is 'AES256'. "
                "SSE-S3 does not use KMS keys."
            )
            raise ValueError(error_message)
        return self


class OLBucket(pulumi.ComponentResource):
    """A component resource for creating and managing S3 buckets with best practices.

    This component encapsulates an S3 bucket and its associated resources including:
    - Versioning configuration
    - Public access blocking
    - Server-side encryption
    - Access logging
    - Lifecycle rules with intelligent tiering for cost optimization
    - CORS configuration
    - Website hosting
    - Object ownership controls

    The component applies secure defaults and enables intelligent tiering by default
    to optimize storage costs automatically.
    """

    bucket_v2: s3.Bucket
    bucket_versioning: s3.BucketVersioning | None = None
    bucket_acl: s3.BucketAcl | None = None
    bucket_public_access_block: s3.BucketPublicAccessBlock | None = None
    bucket_logging: s3.BucketLogging | None = None
    bucket_lifecycle: s3.BucketLifecycleConfiguration | None = None
    bucket_website: s3.BucketWebsiteConfiguration | None = None
    bucket_encryption: s3.BucketServerSideEncryptionConfiguration | None = None
    bucket_cors: s3.BucketCorsConfiguration | None = None
    bucket_ownership_controls: s3.BucketOwnershipControls | None = None
    bucket_policy: s3.BucketPolicy | None = None

    def __init__(
        self,
        name: str,
        config: S3BucketConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        """
        Create an OLBucket component resource.

        :param name: The _unique_ name of the resource.
        :param config: The configuration for the S3 bucket.
        :param opts: A bag of options that control this resource's behavior.
        """
        super().__init__("ol:aws:s3:OLBucket", name, {}, opts)

        child_opts = (opts or pulumi.ResourceOptions()).merge(
            pulumi.ResourceOptions(parent=self)
        )

        # Apply merged tags from config and component name
        # Use config.bucket_name if provided, otherwise default to the Pulumi
        # resource name
        bucket_name_tag = config.bucket_name if config.bucket_name else name
        merged_tags = config.merged_tags({"Name": bucket_name_tag})

        # Create the S3 Bucket resource
        self.bucket_v2 = s3.Bucket(
            f"{name}-bucket",
            bucket=config.bucket_name,  # Use the explicit bucket_name if provided
            force_destroy=config.force_destroy,
            tags=merged_tags,
            opts=child_opts,
        )

        # Conditionally create BucketVersioning
        if config.versioning_enabled:
            self.bucket_versioning = s3.BucketVersioning(
                f"{name}-versioning",
                bucket=self.bucket_v2.id,
                versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
                    status="Enabled"
                ),
                opts=child_opts,
            )

        # Conditionally create BucketAcl
        if config.acl:
            self.bucket_acl = s3.BucketAcl(
                f"{name}-acl",
                bucket=self.bucket_v2.id,
                acl=config.acl,
                opts=child_opts,
            )

        # Always create BucketPublicAccessBlock with the configured values
        # (defaults are True)
        self.bucket_public_access_block = s3.BucketPublicAccessBlock(
            f"{name}-public-access-block",
            bucket=self.bucket_v2.id,
            block_public_acls=config.block_public_acls,
            block_public_policy=config.block_public_policy,
            ignore_public_acls=config.ignore_public_acls,
            restrict_public_buckets=config.restrict_public_buckets,
            opts=child_opts,
        )

        # Conditionally create BucketServerSideEncryptionConfiguration
        if config.server_side_encryption_enabled:
            # Build encryption configuration based on algorithm
            default_args_type = s3.BucketServerSideEncryptionConfigurationRuleApplyServerSideEncryptionByDefaultArgs
            encryption_args = default_args_type(
                sse_algorithm=config.sse_algorithm,
            )
            # Only set KMS key ID if using KMS encryption
            if config.sse_algorithm == "aws:kms":
                encryption_args.kms_master_key_id = config.kms_key_id

            self.bucket_encryption = s3.BucketServerSideEncryptionConfiguration(
                f"{name}-encryption",
                bucket=self.bucket_v2.id,
                rules=[
                    s3.BucketServerSideEncryptionConfigurationRuleArgs(
                        apply_server_side_encryption_by_default=encryption_args,
                        bucket_key_enabled=config.bucket_key_enabled,
                    )
                ],
                opts=child_opts,
            )

        # Conditionally create BucketLogging
        if config.logging_target_bucket:
            self.bucket_logging = s3.BucketLogging(
                f"{name}-logging",
                bucket=self.bucket_v2.id,
                target_bucket=config.logging_target_bucket,
                target_prefix=config.logging_target_prefix,
                opts=child_opts,
            )

        # Create or enhance BucketLifecycleConfiguration
        lifecycle_rules = list(config.lifecycle_rules) if config.lifecycle_rules else []

        # Add intelligent tiering rule if enabled
        if config.intelligent_tiering_enabled:
            intelligent_tiering_rule = s3.BucketLifecycleConfigurationRuleArgs(
                id="intelligent-tiering-transition",
                status="Enabled",
                transitions=[
                    s3.BucketLifecycleConfigurationRuleTransitionArgs(
                        days=config.intelligent_tiering_days,
                        storage_class="INTELLIGENT_TIERING",
                    )
                ],
            )
            lifecycle_rules.append(intelligent_tiering_rule)

        # Only create lifecycle configuration if there are rules
        if lifecycle_rules:
            self.bucket_lifecycle = s3.BucketLifecycleConfiguration(
                f"{name}-lifecycle",
                bucket=self.bucket_v2.id,
                rules=lifecycle_rules,
                opts=child_opts,
            )

        # Conditionally create BucketWebsiteConfiguration
        if config.website_configuration:
            self.bucket_website = s3.BucketWebsiteConfiguration(
                f"{name}-website",
                bucket=self.bucket_v2.id,
                index_document=config.website_configuration.index_document,
                error_document=config.website_configuration.error_document,
                redirect_all_requests_to=config.website_configuration.redirect_all_requests_to,
                routing_rules=config.website_configuration.routing_rules,
                opts=child_opts,
            )

        # Conditionally create BucketCorsConfiguration
        if config.cors_rules:
            self.bucket_cors = s3.BucketCorsConfiguration(
                f"{name}-cors",
                bucket=self.bucket_v2.id,
                cors_rules=config.cors_rules,
                opts=child_opts,
            )

        # Create BucketOwnershipControls
        self.bucket_ownership_controls = s3.BucketOwnershipControls(
            f"{name}-ownership-controls",
            bucket=self.bucket_v2.id,
            rule=s3.BucketOwnershipControlsRuleArgs(
                object_ownership=config.ownership_controls
            ),
            opts=child_opts,
        )

        if config.bucket_policy_document:
            self.bucket_policy = s3.BucketPolicy(
                f"{name}-policy",
                bucket=self.bucket_v2.id,
                policy=config.bucket_policy_document,
                opts=child_opts,
            )

        # Register outputs for the component
        self.register_outputs(
            {
                "arn": self.bucket_v2.arn,
                "name": self.bucket_v2.bucket,
                "id": self.bucket_v2.id,
            }
        )
