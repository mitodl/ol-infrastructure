"""
Pulumi project for MIT OL SFTP Sites
"""

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
        "OU": "mit-open",
        "Environment": stack_info.env_suffix,
        "Application": Services.mit_learn,
    }
)

mit_press_sftp_user_config = SFTPUserConfig(
    username="mit_press",
    public_keys=["Dummy MIT press SFTP key"],
    home_directory="/mit_press",
)

mit_press_sftp_server_config = SFTPServerConfig(
    server_name="mit_press_sftp",
    bucket_name=f"ol-mitlearn-mitpress-sftp-{stack_info.env_suffix}",
    users=[mit_press_sftp_user_config],
    tags=aws_config.tags,
)

mit_press_sftp_server = SFTPServer(
    sftp_config=mit_press_sftp_server_config,
)
