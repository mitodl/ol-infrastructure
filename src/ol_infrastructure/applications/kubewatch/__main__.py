from pulumi import Config

from ol_infrastructure.lib.pulumi_helper import parse_stack

kubewatch_config = Config("kubewatch")
stack_info = parse_stack()
