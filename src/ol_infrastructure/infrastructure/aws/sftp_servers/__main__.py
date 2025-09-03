"""
Pulumi project for MIT OL SFTP Sites
"""

from pulumi import Config

from ol_infrastructure.components.aws.sftp import (
    SFTPServer,
    SFTPServerConfig,
    SFTPUserConfig,
)
from ol_infrastructure.lib.ol_types import AWSBase, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
aws_config = AWSBase(
    tags={
        "OU": "mit-learn",
        "Environment": stack_info.env_suffix,
        "Application": Services.mit_learn,
    }
)

sftp_config = Config("aws_sftp")
mitpress_sftp_user_name = "mitpress"
mitpress_sftp_public_key = sftp_config.require("mitpress_sftp_public_key")

mit_press_sftp_user_config = SFTPUserConfig(
    username=mitpress_sftp_user_name,
    public_keys=[mitpress_sftp_public_key],
)

sftp_server_config = SFTPServerConfig(
    server_name=f"sftp-{stack_info.env_suffix}",
    bucket_name=f"ol-data-lake-sftp-{stack_info.env_suffix}",
    users=[mit_press_sftp_user_config],
    tags=aws_config.tags,
)

sftp_server = SFTPServer(
    sftp_config=sftp_server_config,
)
