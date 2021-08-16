from pulumi import StackReference
from pulumi_consul import (
    PreparedQuery,
    PreparedQueryFailoverArgs,
    PreparedQueryTemplateArgs,
)

from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
operations_vpc = network_stack.require_output("operations_vpc")
vault_operations_query = PreparedQuery(
    "vault-active-server-prepared-query",
    name="vault",
    service="vault",
    tags=["active"],
    failover=PreparedQueryFailoverArgs(
        datacenters=[
            "operations-ci",
            "operations-qa",
            "operations",
        ]
    ),
)

operations_log_service_query = PreparedQuery(
    "operations-log-service-query",
    name="logging",
    service="${match(2)}",
    tags=["logging"],
    failover=PreparedQueryFailoverArgs(
        datacenters=[
            "operations-ci",
            "operations-qa",
            "operations",
        ]
    ),
    template=PreparedQueryTemplateArgs(
        regexp="^(operations|logging)-(.*?)$", type="name_prefix_match"
    ),
)

nearest_service_query = PreparedQuery(
    "neearest-service-query",
    name="nearest",
    service="${match(1)}",
    near="_agent",
    template=PreparedQueryTemplateArgs(
        regexp="^nearest-(.*?)$", type="name_prefix_match"
    ),
)
