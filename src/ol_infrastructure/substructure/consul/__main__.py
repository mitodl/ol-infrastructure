from pulumi import ResourceOptions, StackReference
from pulumi_consul import (
    PreparedQuery,
    PreparedQueryFailoverArgs,
    PreparedQueryTemplateArgs,
)

from ol_infrastructure.lib.consul import get_consul_provider
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
operations_vpc = network_stack.require_output("operations_vpc")
consul_provider = get_consul_provider(stack_info).merge(
    ResourceOptions(delete_before_replace=True)
)

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
            "operations-production",
        ]
    ),
    opts=consul_provider,
)

log_service_query = PreparedQuery(
    "operations-log-service-query",
    name="logging",
    service="${match(2)}",
    tags=["logging"],
    failover=PreparedQueryFailoverArgs(
        datacenters=[
            "operations-ci",
            "operations-qa",
            "operations",
            "operations-production",
        ]
    ),
    template=PreparedQueryTemplateArgs(
        regexp="^(operations|logging)-(.*?)$", type="name_prefix_match"
    ),
    opts=consul_provider,
)

operations_service_query = PreparedQuery(
    "operations-service-query",
    name="operations",
    service="${match(2)}",
    tags=["logging"],
    failover=PreparedQueryFailoverArgs(
        datacenters=[
            "operations-ci",
            "operations-qa",
            "operations",
            "operations-production",
        ]
    ),
    template=PreparedQueryTemplateArgs(
        regexp="^(operations|logging)-(.*?)$", type="name_prefix_match"
    ),
    opts=consul_provider,
)

nearest_service_query = PreparedQuery(
    "nearest-service-query",
    name="nearest",
    service="${match(1)}",
    near="_agent",
    template=PreparedQueryTemplateArgs(
        regexp="^nearest-(.*?)$", type="name_prefix_match"
    ),
    opts=consul_provider,
)
