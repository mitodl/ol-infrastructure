"""
Pulumi project for UAI Partners Storage (S3/SFTP).

This project enables Universal AI partners to consume data via S3 or SFTP,
with each partner having access to only their designated prefix.

Expected configuration format in Pulumi stack YAML:
    config:
      uai_partners:partners:
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
uai_config = Config("uai_partners")

# Base AWS configuration
aws_config = AWSBase(
    tags={
        "OU": "operations",
        "Environment": stack_info.env_suffix,
        "Application": "uai-partners-storage",
    }
)

# Parse partner configurations from Pulumi config
partners_config = uai_config.require_object("partners")

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
bucket_name = f"ol-uai-partners-storage-{stack_info.env_suffix}"
sftp_server_config = SFTPServerConfig(
    server_name=f"uai-partners-sftp-{stack_info.env_suffix}",
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
