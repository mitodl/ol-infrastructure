"""
Manage the creation of VPC infrastructure and the peering relationships between them.

In addition to creating the VPCs, it will also import existing ones based on the defined
CIDR block.  Some of these environments were previously created with SaltStack.  In that
code we defaulted to the first network being at `x.x.1.0/24`, whereas in the OLVPC
component the first subnet defaults to being at `x.x.0.0/24`.  Due to this disparity,
some of the networks defined below specify 4 subnets, which results in a `x.x.0.0/24`
network being created, while also importing the remaining 3 subnets.  If only 3 subnets
were specified then one of the existing networks would not be managed with Pulumi.
"""

from typing import Any, Optional

from pulumi import Config, export
from pulumi_aws import ec2
from security_groups import default_group, public_ssh, public_web, salt_minion

from ol_infrastructure.components.aws.olvpc import (
    OLVPC,
    OLVPCConfig,
    OLVPCPeeringConnection,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack


def vpc_exports(vpc: OLVPC, peers: Optional[list[str]] = None) -> dict[str, Any]:
    """Create a consistent structure for VPC stack exports.

    :param vpc: The VPC whose data you would like to export
    :type vpc: OLVPC

    :param peers: A list of the VPC peers that connect to this network.
    :type peers: Optional[List[Text]]

    :returns: A dictionary of data to be exported

    :rtype: Dict[Text, Any]
    """
    return_value = {
        "cidr": vpc.olvpc.cidr_block,
        "cidr_v6": vpc.olvpc.ipv6_cidr_block,
        "id": vpc.olvpc.id,
        "name": vpc.vpc_config.vpc_name,
        "peers": peers or [],
        "region": vpc.vpc_config.region,
        "rds_subnet": vpc.db_subnet_group.name,
        "elasticache_subnet": vpc.cache_subnet_group.name,
        "subnet_ids": [subnet.id for subnet in vpc.olvpc_subnets],
        "subnet_zones": [subnet.availability_zone for subnet in vpc.olvpc_subnets],
        "route_table_id": vpc.route_table.id,
    }
    if vpc.k8s_service_subnet:
        return_value["k8s_service_subnet"] = vpc.k8s_service_subnet.cidr_block
    return return_value


stack_info = parse_stack()

k8s_config = Config("k8s_vpc")
k8s_vpc_config = OLVPCConfig(
    vpc_name=f"k8s-{stack_info.env_suffix}",
    cidr_block=k8s_config.require("cidr_block"),
    k8s_service_subnet=k8s_config.require("k8s_service_subnet"),
    num_subnets=16,
    tags={
        "OU": "operations",
        "Environment": f"k8s-{stack_info.env_suffix}",
        "business_unit": "operations",
        "Name": f"OL K8S {stack_info.name}",
    },
)
k8s_vpc = OLVPC(k8s_vpc_config)

apps_config = Config("apps_vpc")
applications_vpc_config = OLVPCConfig(
    vpc_name=f"applications-{stack_info.env_suffix}",
    cidr_block=apps_config.require("cidr_block"),
    num_subnets=4,
    tags={
        "OU": "operations",
        "Environment": f"applications-{stack_info.env_suffix}",
        "business_unit": "operations",
        "Name": f"OL Applications {stack_info.name}",
    },
)
applications_vpc = OLVPC(applications_vpc_config)

data_config = Config("data_vpc")
data_vpc_config = OLVPCConfig(
    vpc_name=f"ol-data-{stack_info.env_suffix}",
    cidr_block=data_config.require("cidr_block"),
    num_subnets=3,
    tags={
        "OU": "data",
        "Environment": f"data-{stack_info.env_suffix}",
        "business_unit": "data",
        "Name": f"{stack_info.name} Data Services",
    },
)
data_vpc = OLVPC(data_vpc_config)

ops_config = Config("operations_vpc")
operations_vpc_config = OLVPCConfig(
    vpc_name=ops_config.require("name"),
    cidr_block=ops_config.require("cidr_block"),
    num_subnets=4,
    tags={
        "OU": "operations",
        "Environment": f"operations-{stack_info.env_suffix}",
        "business_unit": "operations",
        "Name": f"Operations {stack_info.name}",
    },
)
operations_vpc = OLVPC(operations_vpc_config)

mitx_config = Config("residential_vpc")
residential_mitx_vpc_config = OLVPCConfig(
    vpc_name=f"mitx-{stack_info.env_suffix}",
    cidr_block=mitx_config.require("cidr_block"),
    num_subnets=4,
    tags={
        "OU": "residential",
        "Environment": f"mitx-{stack_info.env_suffix}",
        "business_unit": "residential",
        "Name": f"MITx {stack_info.name}",
    },
)
residential_mitx_vpc = OLVPC(residential_mitx_vpc_config)

mitx_staging_config = Config("residential_staging_vpc")
residential_mitx_staging_vpc_config = OLVPCConfig(
    vpc_name=f"mitx-staging-{stack_info.env_suffix}",
    cidr_block=mitx_staging_config.require("cidr_block"),
    num_subnets=3,
    tags={
        "OU": "residential-staging",
        "Environment": f"mitx-staging-{stack_info.env_suffix}",
        "business_unit": "residential-staging",
        "Name": f"MITx {stack_info.name} Staging",
    },
)
residential_mitx_staging_vpc = OLVPC(residential_mitx_staging_vpc_config)

mitx_online_config = Config("mitx_online_vpc")
mitx_online_vpc_config = OLVPCConfig(
    vpc_name=f"mitx-online-{stack_info.env_suffix}",
    cidr_block=mitx_online_config.require("cidr_block"),
    num_subnets=5,
    tags={
        "OU": "mitxonline",
        "Environment": f"mitxonline-{stack_info.env_suffix}",
        "business_unit": "mitxonline",
        "Name": f"MITx Online {stack_info.name}",
    },
)
mitx_online_vpc = OLVPC(mitx_online_vpc_config)

xpro_config = Config("xpro_vpc")
xpro_vpc_config = OLVPCConfig(
    vpc_name=f"mitxpro-{stack_info.env_suffix}",
    cidr_block=xpro_config.require("cidr_block"),
    num_subnets=4,
    tags={
        "OU": "mitxpro",
        "Environment": f"mitxpro-{stack_info.env_suffix}",
        "business_unit": "mitxpro",
        "Name": f"xPro {stack_info.name}",
    },
)
xpro_vpc = OLVPC(xpro_vpc_config)

data_vpc_exports = vpc_exports(
    data_vpc,
    [
        "applications_vpc",
        "mitxonline_vpc",
        "operations_vpc",
        "residential_mitx_vpc",
        "residential_mitx_staging_vpc",
        "xpro_vpc",
    ],
)
data_vpc_exports.update(
    {
        "security_groups": {
            "default": data_vpc.olvpc.id.apply(default_group).id,
            "ssh": public_ssh(data_vpc_config.vpc_name, data_vpc.olvpc)(
                tags=data_vpc_config.merged_tags(
                    {"Name": f"ol-data-{stack_info.env_suffix}-public-ssh"}
                ),
                name=f"ol-data-{stack_info.env_suffix}-public-ssh",
            ).id,
            "web": public_web(data_vpc_config.vpc_name, data_vpc.olvpc)(
                tags=data_vpc_config.merged_tags(
                    {"Name": f"ol-data-{stack_info.env_suffix}-public-web"}
                ),
                name=f"ol-data-{stack_info.env_suffix}-public-web",
            ).id,
            "salt_minion": salt_minion(
                data_vpc_config.vpc_name, data_vpc.olvpc, operations_vpc.olvpc
            )(
                tags=data_vpc_config.merged_tags(
                    {"Name": f"ol-data-{stack_info.env_suffix}-salt-minion"}
                ),
                name=f"ol-data-{stack_info.env_suffix}-salt-minion",
            ).id,
            "orchestrator": ec2.SecurityGroup(
                f"{data_vpc_config.vpc_name}-data-orchestrator",
                description="Security group used by the data orchestration engine",
                vpc_id=data_vpc.olvpc.id,
                ingress=[],
                egress=[],
                tags=data_vpc_config.merged_tags(
                    {"Name": f"ol-data-{stack_info.env_suffix}-data-orchestrator"}
                ),
            ).id,
            "integrator": ec2.SecurityGroup(
                f"{data_vpc_config.vpc_name}-data-integrator",
                description="Security group used by the data integration engine",
                vpc_id=data_vpc.olvpc.id,
                ingress=[],
                egress=[],
                tags=data_vpc_config.merged_tags(
                    {"Name": f"ol-data-{stack_info.env_suffix}-data-integrator"}
                ),
            ).id,
        }
    }
)
export("data_vpc", data_vpc_exports)

residential_mitx_vpc_exports = vpc_exports(
    residential_mitx_vpc, ["data_vpc", "operations_vpc"]
)
residential_mitx_vpc_exports.update(
    {
        "security_groups": {
            "default": residential_mitx_vpc.olvpc.id.apply(default_group).id,
            "ssh": public_ssh(
                residential_mitx_vpc_config.vpc_name, residential_mitx_vpc.olvpc
            )(
                tags=residential_mitx_vpc_config.merged_tags(
                    {"Name": f"mitx-{stack_info.env_suffix}-public-ssh"}
                ),
                name=f"mitx-{stack_info.env_suffix}-public-ssh",
            ).id,
            "web": public_web(
                residential_mitx_vpc_config.vpc_name, residential_mitx_vpc.olvpc
            )(
                tags=residential_mitx_vpc_config.merged_tags(
                    {"Name": f"mitx-{stack_info.env_suffix}-public-web"}
                ),
                name=f"mitx-{stack_info.env_suffix}-public-web",
            ).id,
            "salt_minion": salt_minion(
                residential_mitx_vpc_config.vpc_name,
                residential_mitx_vpc.olvpc,
                operations_vpc.olvpc,
            )(
                tags=residential_mitx_vpc_config.merged_tags(
                    {"Name": f"mitx-{stack_info.env_suffix}-salt-minion"}
                ),
                name=f"mitx-{stack_info.env_suffix}-salt-minion",
            ).id,
        }
    }
)
export("residential_mitx_vpc", residential_mitx_vpc_exports)

residential_mitx_staging_vpc_exports = vpc_exports(
    residential_mitx_staging_vpc, ["operations_vpc"]
)
residential_mitx_staging_vpc_exports.update(
    {
        "security_groups": {
            "default": residential_mitx_staging_vpc.olvpc.id.apply(default_group).id,
            "ssh": public_ssh(
                residential_mitx_staging_vpc_config.vpc_name,
                residential_mitx_staging_vpc.olvpc,
            )(
                tags=residential_mitx_staging_vpc_config.merged_tags(
                    {"Name": f"mitx-staging-{stack_info.env_suffix}-public-ssh"}
                ),
                name=f"mitx-staging-{stack_info.env_suffix}-public-ssh",
            ).id,
            "web": public_web(
                residential_mitx_staging_vpc_config.vpc_name,
                residential_mitx_staging_vpc.olvpc,
            )(
                tags=residential_mitx_staging_vpc_config.merged_tags(
                    {"Name": f"mitx-staging-{stack_info.env_suffix}-public-web"}
                ),
                name=f"mitx-staging-{stack_info.env_suffix}-public-web",
            ).id,
            "salt_minion": salt_minion(
                residential_mitx_staging_vpc_config.vpc_name,
                residential_mitx_staging_vpc.olvpc,
                operations_vpc.olvpc,
            )(
                tags=residential_mitx_staging_vpc_config.merged_tags(
                    {"Name": f"mitx-staging-{stack_info.env_suffix}-salt-minion"}
                ),
                name=f"mitx-staging-{stack_info.env_suffix}-salt-minion",
            ).id,
        }
    }
)
export("residential_mitx_staging_vpc", residential_mitx_staging_vpc_exports)

mitx_online_vpc_exports = vpc_exports(mitx_online_vpc, ["data_vpc", "operations_vpc"])
mitx_online_vpc_exports.update(
    {
        "security_groups": {
            "default": mitx_online_vpc.olvpc.id.apply(default_group).id,
            "ssh": public_ssh(mitx_online_vpc_config.vpc_name, mitx_online_vpc.olvpc)(
                tags=mitx_online_vpc_config.merged_tags(
                    {"Name": f"mitxonline-{stack_info.env_suffix}-public-ssh"}
                ),
                name=f"mitxonline-{stack_info.env_suffix}-public-ssh",
            ).id,
            "web": public_web(mitx_online_vpc_config.vpc_name, mitx_online_vpc.olvpc)(
                tags=mitx_online_vpc_config.merged_tags(
                    {"Name": f"mitxonline-{stack_info.env_suffix}-public-web"}
                ),
                name=f"mitxonline-{stack_info.env_suffix}-public-web",
            ).id,
            "salt_minion": salt_minion(
                mitx_online_vpc_config.vpc_name,
                mitx_online_vpc.olvpc,
                operations_vpc.olvpc,
            )(
                tags=mitx_online_vpc_config.merged_tags(
                    {"Name": f"mitxonline-{stack_info.env_suffix}-salt-minion"}
                ),
                name=f"mitxonline-{stack_info.env_suffix}-salt-minion",
            ).id,
        }
    }
)
export("mitxonline_vpc", mitx_online_vpc_exports)

xpro_vpc_exports = vpc_exports(xpro_vpc, ["data_vpc", "operations_vpc"])
xpro_vpc_exports.update(
    {
        "security_groups": {
            "default": xpro_vpc.olvpc.id.apply(default_group).id,
            "web": public_web(xpro_vpc_config.vpc_name, xpro_vpc.olvpc)(
                tags=xpro_vpc_config.merged_tags(
                    {"Name": f"mitxpro-{stack_info.env_suffix}-public-web"}
                ),
                name=f"mitxpro-{stack_info.env_suffix}-public-web",
            ).id,
            "ssh": public_ssh(xpro_vpc_config.vpc_name, xpro_vpc.olvpc)(
                tags=xpro_vpc_config.merged_tags(
                    {"Name": f"mitxpro-{stack_info.env_suffix}-public-ssh"}
                ),
                name=f"mitxpro-{stack_info.env_suffix}-public-ssh",
            ).id,
            "salt_minion": salt_minion(
                xpro_vpc_config.vpc_name,
                xpro_vpc.olvpc,
                operations_vpc.olvpc,
            )(
                tags=xpro_vpc_config.merged_tags(
                    {"Name": f"mitxpro-{stack_info.env_suffix}-salt-minion"}
                ),
                name=f"mitxpro-{stack_info.env_suffix}-salt-minion",
            ).id,
        }
    }
)
export("xpro_vpc", xpro_vpc_exports)

# TODO: MD 2022-05-13 This probably needs to be expanded upon once the k8s network is peered to others  # noqa: E501, FIX002, TD002, TD003
# when it gains some security groups.
k8s_vpc_exports = vpc_exports(k8s_vpc)
export("k8s_vpc", k8s_vpc_exports)

applications_vpc_exports = vpc_exports(applications_vpc, ["data_vpc", "operations_vpc"])
applications_vpc_exports.update(
    {
        "security_groups": {
            "default": applications_vpc.olvpc.id.apply(default_group).id,
            "web": public_web(applications_vpc_config.vpc_name, applications_vpc.olvpc)(
                tags=applications_vpc_config.merged_tags(
                    {"Name": f"applications-{stack_info.env_suffix}-public-web"}
                ),
                name=f"applications-{stack_info.env_suffix}-public-web",
            ).id,
            "ssh": public_ssh(applications_vpc_config.vpc_name, applications_vpc.olvpc)(
                tags=applications_vpc_config.merged_tags(
                    {"Name": f"applications-{stack_info.env_suffix}-public-ssh"}
                ),
                name=f"applications-{stack_info.env_suffix}-public-ssh",
            ).id,
            "salt_minion": salt_minion(
                applications_vpc_config.vpc_name,
                applications_vpc.olvpc,
                operations_vpc.olvpc,
            )(
                tags=applications_vpc_config.merged_tags(
                    {"Name": f"applications-{stack_info.env_suffix}-salt-minion"}
                ),
                name=f"applications-{stack_info.env_suffix}-salt-minion",
            ).id,
        }
    }
)
export("applications_vpc", applications_vpc_exports)

operations_vpc_exports = vpc_exports(
    operations_vpc,
    [
        "applications_vpc",
        "data_vpc",
        "mitxonline_vpc",
        "residential_mitx_vpc",
        "residential_mitx_staging_vpc",
        "xpro_vpc",
    ],
)
operations_vpc_exports.update(
    {
        "security_groups": {
            "celery_monitoring": ec2.SecurityGroup(
                "operations-celery-monitoring",
                description="Security group for Leek service for Celery Monitoring",
                vpc_id=operations_vpc.olvpc.id,
                ingress=[],
                egress=[],
                tags=operations_vpc_config.merged_tags(
                    {"Name": f"ol-operations-{stack_info.env_suffix}-celery-monitoring"}
                ),
            ).id,
            "default": operations_vpc.olvpc.id.apply(default_group).id,
            "web": public_web(operations_vpc_config.vpc_name, operations_vpc.olvpc)(
                tags=operations_vpc_config.merged_tags(
                    {"Name": f"operations-{stack_info.env_suffix}-public-web"}
                ),
                name=f"operations-{stack_info.env_suffix}-public-web",
            ).id,
            "ssh": public_ssh(operations_vpc_config.vpc_name, operations_vpc.olvpc)(
                tags=operations_vpc_config.merged_tags(
                    {"Name": f"operations-{stack_info.env_suffix}-public-ssh"}
                ),
                name=f"operations-{stack_info.env_suffix}-public-ssh",
            ).id,
            "salt_minion": salt_minion(
                operations_vpc_config.vpc_name,
                operations_vpc.olvpc,
                operations_vpc.olvpc,
            )(
                tags=data_vpc_config.merged_tags(
                    {"Name": f"operations-{stack_info.env_suffix}-salt-minion"}
                ),
                name=f"operations-{stack_info.env_suffix}-salt-minion",
            ).id,
        }
    }
)
export("operations_vpc", operations_vpc_exports)

data_to_mitx_online_peer = OLVPCPeeringConnection(
    f"ol-data-{stack_info.env_suffix}-to-mitx-online-{stack_info.env_suffix}-vpc-peer",
    data_vpc,
    mitx_online_vpc,
)
data_to_mitx_peer = OLVPCPeeringConnection(
    "ol-data-{0}-to-residential-mitx-{0}-vpc-peer".format(stack_info.env_suffix),
    data_vpc,
    residential_mitx_vpc,
)
data_to_mitx_staging_peer = OLVPCPeeringConnection(
    "ol-data-{0}-to-residential-mitx-staging-{0}-vpc-peer".format(
        stack_info.env_suffix
    ),
    data_vpc,
    residential_mitx_staging_vpc,
)
data_to_applications_peer = OLVPCPeeringConnection(
    f"ol-data-{stack_info.env_suffix}-to-applications-{stack_info.env_suffix}-vpc-peer",
    data_vpc,
    applications_vpc,
)
data_to_xpro_peer = OLVPCPeeringConnection(
    f"ol-data-{stack_info.env_suffix}-to-mitxpro-{stack_info.env_suffix}-vpc-peer",
    data_vpc,
    xpro_vpc,
)
operations_to_applications_peer = OLVPCPeeringConnection(
    "ol-operations-{0}-to-applications-{0}-vpc-peer".format(stack_info.env_suffix),
    operations_vpc,
    applications_vpc,
)
operations_to_data_peer = OLVPCPeeringConnection(
    f"ol-operations-{stack_info.env_suffix}-to-ol-data-{stack_info.env_suffix}-vpc-peer",
    operations_vpc,
    data_vpc,
)
operations_to_mitx_online_peer = OLVPCPeeringConnection(
    "ol-operations-{0}-to-mitx-online-{0}-vpc-peer".format(stack_info.env_suffix),
    operations_vpc,
    mitx_online_vpc,
)
operations_to_mitx_peer = OLVPCPeeringConnection(
    "ol-operations-{0}-to-residential-mitx-{0}-vpc-peer".format(stack_info.env_suffix),
    operations_vpc,
    residential_mitx_vpc,
)
operations_to_mitx_staging_peer = OLVPCPeeringConnection(
    "ol-operations-{0}-to-residential-mitx-staging-{0}-vpc-peer".format(
        stack_info.env_suffix
    ),
    operations_vpc,
    residential_mitx_staging_vpc,
)
operations_to_xpro_peer = OLVPCPeeringConnection(
    f"ol-operations-{stack_info.env_suffix}-to-mitxpro-{stack_info.env_suffix}-vpc-peer",
    operations_vpc,
    xpro_vpc,
)
