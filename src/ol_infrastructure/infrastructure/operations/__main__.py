import importlib

from pulumi import Config, export

from ol_infrastructure.infrastructure import operations as ops

env_config = Config("environment")
environment_name = f"{ops.env_prefix}-{ops.env_suffix}"
business_unit = env_config.get("business_unit") or "operations"
service_list = env_config.require_object("services")

security_groups = {}

for service in service_list:
    module = importlib.import_module(service)
    security_groups.update(getattr(module, "security_groups", {}))
    export(service, getattr(module, "export_data", {}))

export("security_groups", security_groups)
