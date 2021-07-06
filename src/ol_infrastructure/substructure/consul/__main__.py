from pulumi_consul import PreparedQuery, PreparedQueryFailoverArgs

from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()

vault_operations_query = PreparedQuery(
    "vault-active-server-prepared-query",
    name="vault",
    service="vault",
    tags=["active"],
    failover=PreparedQueryFailoverArgs(
        datacenters=["operations-ci", "operations-qa", "operations"]
    ),
)
