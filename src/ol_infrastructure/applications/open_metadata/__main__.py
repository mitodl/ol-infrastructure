from pulumi import Config

from ol_infrastructure.lib.pulumi_helper import parse_stack

open_metadata_config = Config("open_metadata")
stack_info = parse_stack()
