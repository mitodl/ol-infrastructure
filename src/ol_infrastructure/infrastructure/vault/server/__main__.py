from pulumi import Config

from ol_infrastructure.lib.pulumi_helper import parse_stack

vault_server_config = Config("vault")
stack_info = parse_stack()
