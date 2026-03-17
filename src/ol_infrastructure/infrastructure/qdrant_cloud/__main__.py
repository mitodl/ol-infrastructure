from collections.abc import Sequence
from pathlib import Path

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
provider_opts = ResourceOptions(provider=provider)
invoke_opts = InvokeOptions(provider=provider)

cloud_provider = qdrant_cloud_config.get("cloud_provider") or "aws"
cloud_region = qdrant_cloud_config.get("cloud_region") or "us-east-1"
number_of_nodes = qdrant_cloud_config.get_int("number_of_nodes") or 3
cluster_version = qdrant_cloud_config.get("cluster_version") or QDRANT_VERSION
enable_high_availability = (
    qdrant_cloud_config.get_bool("enable_high_availability") or False
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

# Build optional additional-disk resource_configurations.
# Each entry in available_additional_resources represents one slider step;
# the amount we pass is how many GiB above the base package disk we want.
node_resource_configurations = None
if desired_disk_gib is not None and package.resource_configurations:
    base_disk_gib = _parse_gib(package.resource_configurations[0].disk)
    effective_disk_gib = max(desired_disk_gib, base_disk_gib)
    if effective_disk_gib > base_disk_gib:
        if not package.available_additional_resources:
            msg = (
                f"Package {desired_package_name!r} does not support additional "
                "disk storage (no available_additional_resources)."
            )
            raise ValueError(msg)
        node_resource_configurations = [
            qdrant_cloud.AccountsClusterConfigurationNodeConfigurationResourceConfigurationArgs(
                amount=effective_disk_gib - base_disk_gib,
                resource_type="disk",
                resource_unit="GiB",
            )
        ]

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
        database_configuration=qdrant_cloud.AccountsClusterConfigurationDatabaseConfigurationArgs(
            collection=qdrant_cloud.AccountsClusterConfigurationDatabaseConfigurationCollectionArgs(
                replication_factor=2 if enable_high_availability else 1,
                write_consistency_factor=1,
            ),
        )
        if enable_high_availability
        else None,
    ),
    account_id=qdrant_secrets["account_id"],
    name=f"mitlearn-{stack_info.env_suffix}",
    opts=provider_opts,
)

export("cluster_url", cluster.url)
export("cluster_id", cluster.id)
