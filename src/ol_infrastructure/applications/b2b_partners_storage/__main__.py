"""
Pulumi project for B2B Partners Storage (S3/SFTP).

This project enables B2B partners to consume data via S3 or SFTP,
with each partner having access to only their designated prefix.

Expected configuration format in Pulumi stack YAML:
    config:
      b2b_partners:partners:
        - name: "partner1"
          username: "partner1_user"
          aws_account_id: "123456789012"
          ssh_public_key: "ssh-rsa ..."
        - name: "partner2"
          username: "partner2_user"
          aws_account_id: "987654321098"
          ssh_public_key: "ssh-rsa ..."
"""

from pulumi import Config, export

from ol_infrastructure.components.aws.sftp import (
    SFTPServer,
    SFTPServerConfig,
    SFTPUserConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
b2b_config = Config("b2b_partners")

# Base AWS configuration
aws_config = AWSBase(
    tags={
        "OU": "operations",
        "Environment": stack_info.env_suffix,
        "Application": "b2b-partners-storage",
    }
)

# Parse partner configurations from Pulumi config
partners_config = b2b_config.require_object("partners")

# Create SFTP user configurations for each partner
sftp_users = [
    SFTPUserConfig(
        username=partner["username"],
        aws_account_id=partner["aws_account_id"],
        public_keys=[partner["ssh_public_key"]],
    )
    for partner in partners_config
]

# Create SFTP server with S3 backend
bucket_name = f"ol-b2b-partners-storage-{stack_info.env_suffix}"
sftp_server_config = SFTPServerConfig(
    server_name=f"b2b-partners-sftp-{stack_info.env_suffix}",
    bucket_name=bucket_name,
    users=sftp_users,
    tags=aws_config.tags,
)

sftp_server = SFTPServer(
    sftp_config=sftp_server_config,
)

# Export useful information
export("sftp_server_id", sftp_server.transfer_server.id)
export("sftp_endpoint", sftp_server.transfer_server.endpoint)
export("bucket_name", sftp_server.bucket.id)
export(
    "partner_usernames",
    [user["username"] for user in partners_config],
)
