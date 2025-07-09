"""Module for creating and managing AWS Transfer Family SFTP servers backed by S3."""

import json
from typing import Literal

from pulumi import ComponentResource, ResourceOptions
from pulumi_aws import iam, s3, transfer
from pydantic import BaseModel, ConfigDict, Field

from ol_infrastructure.lib.ol_types import AWSBase


class SFTPUserConfig(BaseModel):
    """Configuration for SFTP users."""

    username: str
    role_arn: str | None = None
    public_keys: list[str] = Field(default_factory=list)
    model_config = ConfigDict(arbitrary_types_allowed=True)


class SFTPServerConfig(AWSBase):
    """Configuration object for customizing an SFTP server backed by S3."""

    server_name: str
    bucket_name: str
    domain: Literal["S3", "EFS"] = "S3"
    endpoint_type: Literal["PUBLIC", "VPC", "VPC_ENDPOINT"] = "PUBLIC"
    identity_provider_type: Literal[
        "SERVICE_MANAGED", "AWS_LAMBDA", "API_GATEWAY", "AWS_DIRECTORY_SERVICE"
    ] = "SERVICE_MANAGED"
    users: list[SFTPUserConfig] = Field(default_factory=list)
    security_policy_name: str = "TransferSecurityPolicy-2024-01"


class SFTPServer(ComponentResource):
    """A Pulumi component for constructing AWS Transfer Family SFTP server
    backed by S3.
    """

    def __init__(
        self, sftp_config: SFTPServerConfig, opts: ResourceOptions | None = None
    ):
        """Create an SFTP server with S3 backend, IAM roles, and user management.

        :param sftp_config: Configuration object for customizing the component
        :type sftp_config: SFTPServerConfig

        :param opts: Pulumi resource options
        :type opts: ResourceOptions

        :rtype: SFTPServer
        """
        super().__init__(
            "ol:infrastructure:aws:SFTPServer", sftp_config.server_name, None, opts
        )

        generic_resource_opts = ResourceOptions(parent=self).merge(opts)

        # Create S3 bucket for SFTP backend
        self.bucket = s3.BucketV2(
            f"{sftp_config.server_name}-sftp-bucket",
            bucket=sftp_config.bucket_name,
            tags=sftp_config.tags,
            opts=generic_resource_opts,
        )

        # Enable versioning on the bucket
        s3.BucketVersioningV2(
            f"{sftp_config.server_name}-sftp-bucket-versioning",
            bucket=self.bucket.id,
            versioning_configuration=s3.BucketVersioningV2VersioningConfigurationArgs(
                status="Enabled"
            ),
            opts=generic_resource_opts,
        )

        # Block public access to the bucket
        s3.BucketPublicAccessBlock(
            f"{sftp_config.server_name}-sftp-bucket-public-access-block",
            bucket=self.bucket.id,
            block_public_acls=True,
            block_public_policy=True,
            ignore_public_acls=True,
            restrict_public_buckets=True,
            opts=generic_resource_opts,
        )

        # Create Transfer Family server
        self.transfer_server = transfer.Server(
            f"{sftp_config.server_name}",
            domain=sftp_config.domain,
            endpoint_type=sftp_config.endpoint_type,
            identity_provider_type=sftp_config.identity_provider_type,
            protocols=["SFTP"],
            security_policy_name=sftp_config.security_policy_name,
            tags=sftp_config.merged_tags({"Name": f"{sftp_config.server_name}"}),
            opts=generic_resource_opts,
        )

        # Create users if specified
        self.users = []
        for user_config in sftp_config.users:
            # Create user-specific IAM role if not provided
            if not user_config.role_arn:
                user_policy = iam.Policy(
                    f"{user_config.username}-sftp-iam-policy",
                    policy=json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Sid": "AllowListingOfUserFolder",
                                    "Action": ["s3:ListBucket"],
                                    "Effect": "Allow",
                                    "Resource": [
                                        f"arn:aws:s3:::{sftp_config.bucket_name}"
                                    ],
                                },
                                {
                                    "Sid": "HomeDirObjectAccess",
                                    "Effect": "Allow",
                                    "Action": [
                                        "s3:PutObject",
                                        "s3:GetObject",
                                        "s3:GetObjectTagging",
                                        "s3:DeleteObject",
                                        "s3:DeleteObjectVersion",
                                        "s3:GetObjectVersion",
                                        "s3:GetObjectVersionTagging",
                                        "s3:GetObjectACL",
                                        "s3:PutObjectACL",
                                    ],
                                    "Resource": f"arn:aws:s3:::{sftp_config.bucket_name}/{user_config.username}/*",  # noqa: E501
                                },
                            ],
                        }
                    ),
                )

                user_role = iam.Role(
                    f"{sftp_config.server_name}-sftp-{user_config.username}-role",
                    assume_role_policy=json.dumps(
                        {
                            "Version": "2012-10-17",
                            "Statement": [
                                {
                                    "Effect": "Allow",
                                    "Principal": {"Service": "transfer.amazonaws.com"},
                                    "Action": "sts:AssumeRole",
                                }
                            ],
                        }
                    ),
                    tags=sftp_config.merged_tags(
                        {
                            "Name": (
                                f"{sftp_config.server_name}-sftp-user-"
                                f"{user_config.username}-role"
                            ),
                            "SFTPUser": user_config.username,
                        }
                    ),
                    opts=generic_resource_opts,
                )

                # Attach S3 access policy to user role
                iam.RolePolicyAttachment(
                    f"{sftp_config.server_name}-sftp-user-{user_config.username}-policy-attachment",
                    role=user_role.name,
                    policy_arn=user_policy.arn,
                    opts=generic_resource_opts,
                )

                user_role_arn = user_role.arn
            else:
                user_role_arn = user_config.role_arn

            # Create SFTP user
            sftp_user = transfer.User(
                f"{sftp_config.server_name}-sftp-user-{user_config.username}",
                server_id=self.transfer_server.id,
                user_name=user_config.username,
                home_directory_type="LOGICAL",
                home_directory_mappings=[
                    transfer.UserHomeDirectoryMappingArgs(
                        entry="/",
                        target=f"/{sftp_config.bucket_name}/{user_config.username}",
                    )
                ],
                role=user_role_arn,
                tags=sftp_config.merged_tags(
                    {
                        "Name": (
                            f"{sftp_config.server_name}-sftp-user-{user_config.username}"
                        ),
                        "SFTPUser": user_config.username,
                    }
                ),
                opts=generic_resource_opts,
            )

            # Add SSH public keys if provided
            for i, public_key in enumerate(user_config.public_keys):
                transfer.SshKey(
                    f"{sftp_config.server_name}-sftp-user-{user_config.username}-key-{i}",
                    server_id=self.transfer_server.id,
                    user_name=sftp_user.user_name,
                    body=public_key,
                    opts=generic_resource_opts,
                )

            self.users.append(sftp_user)
