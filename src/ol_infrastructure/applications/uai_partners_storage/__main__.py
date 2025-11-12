from pulumi import Config

from ol_infrastructure.lib.pulumi_helper import parse_stack

UAIPartnersStoraged_config = Config("UAIPartnersStoraged")
stack_info = parse_stack()
