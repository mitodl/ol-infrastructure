from pulumi import Config

from ol_infrastructure.lib.pulumi_helper import parse_stack

xpro_dns_config = Config("xpro_dns")
stack_info = parse_stack()
