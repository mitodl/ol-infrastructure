from collections.abc import Sequence
from pathlib import Path
from typing import Literal

import pulumi_qdrant_cloud as qdrant_cloud
from pulumi import Config, InvokeOptions, ResourceOptions, export
from pulumi_qdrant_cloud.outputs import GetBookingPackagesPackageResult

from bridge.lib.versions import QDRANT_VERSION
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.pulumi_helper import parse_stack

qdrant_cloud_config = Config("qdrant-cloud")
stack_info = parse_stack()

qdrant_secrets = read_yaml_secrets(Path("qdrant_cloud/account.yaml"))

provider = qdrant_cloud.Provider(
    "qdrant-cloud-provider",
    api_key=qdrant_secrets["cloud_management_key"],
    account_id=qdrant_secrets["account_id"],
)
invoke_opts = InvokeOptions(provider=provider)

cloud_provider = qdrant_cloud_config.get("cloud_provider") or "aws"
cloud_region = qdrant_cloud_config.get("cloud_region") or "us-east-1"
number_of_nodes = qdrant_cloud_config.get_int("number_of_nodes") or 3
cluster_version = qdrant_cloud_config.get("cluster_version") or QDRANT_VERSION
enable_high_availability = (
    qdrant_cloud_config.get_bool("enable_high_availability") or False
)
storage_type: Literal["balanced", "cost-optimized", "performance"] = (
    qdrant_cloud_config.get("storage_type") or "cost-optimized"
)

# Package name from the Qdrant Cloud booking catalogue for the target region.
# Run `get_booking_packages` (or check the Qdrant Cloud UI) to list available
# names. Default "mx2" = 1000m vCPU / 8Gi RAM / 32Gi disk on AWS us-east-1.
desired_package_name = qdrant_cloud_config.get("desired_package_name") or "mx2"

# Optional target total disk per node in GiB. When set to a value larger than
# the base package disk, additional disk slabs are requested via
# resource_configurations (the "disk slider" in the Qdrant Cloud UI). Each slab
# is represented by one entry in the package's available_additional_resources.
desired_disk_gib = qdrant_cloud_config.get_int("desired_disk_gib")

# Optional target total RAM per node in GiB. When set to a value larger than
# the base package RAM, additional memory slabs are requested via
# resource_configurations (the "memory slider" in the Qdrant Cloud UI).
desired_ram_gib = qdrant_cloud_config.get_int("desired_ram_gib")

booking_result = qdrant_cloud.get_booking_packages(
    cloud_provider=cloud_provider,
    cloud_region=cloud_region,
    opts=invoke_opts,
)


def _find_package(
    packages: Sequence[GetBookingPackagesPackageResult],
    name: str,
) -> GetBookingPackagesPackageResult:
    """Return the package matching the given catalogue name."""
    for pkg in packages:
        if pkg.name == name:
            return pkg

    available = [
        f"{p.name!r} (cpu={p.resource_configurations[0].cpu}, "
        f"ram={p.resource_configurations[0].ram}, "
        f"disk={p.resource_configurations[0].disk})"
        for p in packages
        if p.resource_configurations
    ]
    msg = (
        f"No Qdrant package named {name!r} found for "
        f"{cloud_provider}/{cloud_region}. "
        f"Available packages: {', '.join(available)}"
    )
    raise ValueError(msg)


def _parse_gib(quantity: str) -> int:
    """Parse a Kubernetes-style Gi quantity string (e.g. '32Gi') to an integer."""
    return int(quantity.removesuffix("Gi"))


package = _find_package(booking_result.packages, desired_package_name)

# Build optional additional resource_configurations for disk and/or RAM.
# Each entry in available_additional_resources represents one slider step;
# the amount we pass is how many GiB above the base package value we want.
node_resource_configurations: (
    list[
        qdrant_cloud.AccountsClusterConfigurationNodeConfigurationResourceConfigurationArgs
    ]
    | None
) = None

if package.resource_configurations:
    extra_resources: list[
        qdrant_cloud.AccountsClusterConfigurationNodeConfigurationResourceConfigurationArgs
    ] = []

    if desired_disk_gib is not None:
        base_disk_gib = _parse_gib(package.resource_configurations[0].disk)
        effective_disk_gib = desired_disk_gib - base_disk_gib
        if effective_disk_gib > 0:
            if not package.available_additional_resources:
                msg = (
                    f"Package {desired_package_name!r} does not support additional "
                    "disk storage (no available_additional_resources)."
                )
                raise ValueError(msg)
            extra_resources.append(
                qdrant_cloud.AccountsClusterConfigurationNodeConfigurationResourceConfigurationArgs(
                    amount=effective_disk_gib,
                    resource_type="disk",
                    resource_unit="Gi",
                )
            )

    if desired_ram_gib is not None:
        base_ram_gib = _parse_gib(package.resource_configurations[0].ram)
        effective_ram_gib = desired_ram_gib - base_ram_gib
        if effective_ram_gib > 0:
            if not package.available_additional_resources:
                msg = (
                    f"Package {desired_package_name!r} does not support additional "
                    "RAM (no available_additional_resources)."
                )
                raise ValueError(msg)
            extra_resources.append(
                qdrant_cloud.AccountsClusterConfigurationNodeConfigurationResourceConfigurationArgs(
                    amount=effective_ram_gib,
                    resource_type="ram",
                    resource_unit="Gi",
                )
            )

    if extra_resources:
        node_resource_configurations = extra_resources

storage_type_map = {
    "balanced": qdrant_cloud.AccountsClusterConfigurationDatabaseConfigurationStorageArgs(  # noqa: E501
        performance=qdrant_cloud.AccountsClusterConfigurationDatabaseConfigurationStoragePerformanceArgs(
            optimizer_cpu_budget=0,
            async_scorer=True,
        )
    ),
    "cost-optimized": None,
}

cluster = qdrant_cloud.AccountsCluster(
    f"mitlearn-qdrant-{stack_info.env_suffix}",
    cloud_provider=cloud_provider,
    cloud_region=cloud_region,
    configuration=qdrant_cloud.AccountsClusterConfigurationArgs(
        node_configuration=qdrant_cloud.AccountsClusterConfigurationNodeConfigurationArgs(
            package_id=package.id,
            resource_configurations=node_resource_configurations,
        ),
        number_of_nodes=number_of_nodes,
        version=cluster_version,
        # restart_policy, rebalance_strategy, and service_type are all defined as
        # Optional (not Computed) in the provider schema, so it treats unset values
        # as "" / 0 rather than preserving what the API returns. The flatten path
        # always stores the API values, creating a perpetual diff on every run.
        # Set them explicitly to their managed-cloud defaults to eliminate the diff.
        restart_policy="CLUSTER_CONFIGURATION_RESTART_POLICY_AUTOMATIC",
        rebalance_strategy="CLUSTER_CONFIGURATION_REBALANCE_STRATEGY_BY_COUNT_AND_SIZE",
        service_type="CLUSTER_SERVICE_TYPE_CLUSTER_IP",
        database_configuration=qdrant_cloud.AccountsClusterConfigurationDatabaseConfigurationArgs(
            collection=qdrant_cloud.AccountsClusterConfigurationDatabaseConfigurationCollectionArgs(
                replication_factor=2 if enable_high_availability else 1,
                write_consistency_factor=1,
            ),
            storage=storage_type_map[storage_type],
        )
        if enable_high_availability
        else None,
    ),
    account_id=qdrant_secrets["account_id"],
    name=f"mitlearn-{stack_info.env_suffix}",
    opts=ResourceOptions(
        provider=provider,
        # package_id is looked up dynamically from the booking catalogue; ignore
        # it so catalogue refreshes don't trigger cluster updates.
        #
        # database_configuration is also Optional (not Computed) in the schema,
        # so the provider treats our unset value as [] and diffs it against the
        # rich object the API always returns (inference.enabled, service.apiKey
        # wired up by AccountsDatabaseApiKeyV2, etc.). Sending DatabaseConfiguration=nil
        # in the resulting update causes a 500 from the Qdrant Cloud API.
        # We ignore it here so Pulumi preserves the current API state on updates.
        ignore_changes=[
            "configuration.nodeConfiguration.packageId",
            "configuration.databaseConfiguration",
        ],
    ),
)

export("cluster_url", cluster.url)
export("cluster_id", cluster.id)
