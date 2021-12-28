from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pulumi
import pulumi_aws as aws
import pulumi_consul as consul
import pulumi_mongodbatlas as atlas

from bridge.lib.magic_numbers import DEFAULT_MONGODB_PORT
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

###############
# STACK SETUP #
###############
atlas_config = pulumi.Config("mongodb_atlas")
env_config = pulumi.Config("environment")
stack_info = parse_stack()
network_stack = pulumi.StackReference(f"infrastructure.aws.network.{stack_info.name}")
consul_stack = pulumi.StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)

#############
# VARIABLES #
#############
environment_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc = network_stack.require_output(env_config.require("target_vpc"))
data_vpc = network_stack.require_output("data_vpc")
business_unit = env_config.get("business_unit") or "operations"
aws_config = AWSBase(tags={"OU": business_unit, "Environment": environment_name})
max_disk_size = atlas_config.get_int("disk_autoscale_max_gb")
max_instance_type = atlas_config.get("cluster_autoscale_max_size")
min_instance_type = atlas_config.get("cluster_autoscale_min_size")
num_instances = atlas_config.get_int("cluster_instance_count") or 3
atlas_creds = read_yaml_secrets(Path("pulumi/mongodb_atlas.yaml"))
atlas_provider = pulumi.ResourceOptions(
    provider=atlas.Provider(
        "mongodb-atlas-provider",
        private_key=atlas_creds["private_key"],
        public_key=atlas_creds["public_key"],
    ),
)

#################
# ATLAS PROJECT #
#################
atlas_project = atlas.Project(
    f"mongo-atlas-project-{environment_name}",
    name=environment_name,
    org_id=atlas_config.require("organization_id"),
    opts=atlas_provider,
)

atlas_cluster = atlas.Cluster(
    f"mongo-atlas-cluster-{environment_name}",
    name=f"{business_unit}-{environment_name}",
    auto_scaling_compute_enabled=bool(max_instance_type),
    auto_scaling_compute_scale_down_enabled=bool(min_instance_type),
    auto_scaling_disk_gb_enabled=bool(max_disk_size),
    provider_name="AWS",
    cloud_backup=True,
    cluster_type="REPLICASET",
    disk_size_gb=atlas_config.get_int("disk_size_gb"),
    mongo_db_major_version=atlas_config.get("version") or "4.4",
    pit_enabled=True,
    project_id=atlas_project.id,
    provider_instance_size_name=atlas_config.get("instance_size") or "M10",
    provider_region_name=atlas_config.get("cloud_region") or "US_EAST_1",
    provider_auto_scaling_compute_max_instance_size=max_instance_type,
    provider_auto_scaling_compute_min_instance_size=min_instance_type,
    replication_factor=num_instances,
    opts=atlas_provider,
)

atlas_security_group = aws.ec2.SecurityGroup(
    f"mongodb-atlas-{environment_name}",
    name=f"mongodb-atlas-{environment_name}",
    description=f"Access control to Mongodb Atlas instances in {environment_name}",
    tags=aws_config.merged_tags({"Name": f"{environment_name}-mongodb-atlas"}),
    vpc_id=target_vpc["id"],
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_MONGODB_PORT,
            to_port=DEFAULT_MONGODB_PORT,
            cidr_blocks=[target_vpc["cidr"]],
            ipv6_cidr_blocks=[target_vpc["cidr_v6"]],
            description=f"Access to Mongodb cluster from {environment_name}",
        ),
    ],
    egress=default_egress_args,
)

# It is necessary to manually go to the Mongo Atlas UI and fetch the IP CIDR for adding
# to the VPC route table
atlas_aws_network_peer = atlas.NetworkPeering(
    f"mongo-atlas-network-peering-{environment_name}",
    accepter_region_name=target_vpc["region"],
    container_id=atlas_cluster.container_id,
    vpc_id=target_vpc["id"],
    aws_account_id=aws.get_caller_identity().account_id,
    project_id=atlas_project.id,
    provider_name="AWS",
    route_table_cidr_block=target_vpc["cidr"],
    opts=atlas_provider,
)

accept_atlas_network_peer = aws.ec2.VpcPeeringConnectionAccepter(
    "mongo-atlas-peering-connection-accepter",
    vpc_peering_connection_id=atlas_aws_network_peer.connection_id,
    auto_accept=True,
    accepter=aws.ec2.VpcPeeringConnectionAccepterAccepterArgs(
        allow_remote_vpc_dns_resolution=True,
    ),
    tags=aws_config.tags,
)

atlas_secgroup_network_access = atlas.ProjectIpAccessList(
    "mongo-atlas-network-security-group-permissions",
    aws_security_group=atlas_security_group.id,
    project_id=atlas_project.id,
    opts=pulumi.ResourceOptions(
        depends_on=[atlas_aws_network_peer, accept_atlas_network_peer]
    ).merge(atlas_provider),
)

atlas_cidr_network_access = atlas.ProjectIpAccessList(
    "mongo-atlas-network-cidr-block-permissions",
    project_id=atlas_project.id,
    cidr_block=target_vpc["cidr"],
    opts=pulumi.ResourceOptions(
        depends_on=[atlas_aws_network_peer, accept_atlas_network_peer]
    ).merge(atlas_provider),
)

aws.ec2.Route(
    "mongo-atlas-network-route",
    route_table_id=target_vpc["route_table_id"],
    destination_cidr_block=atlas_config.get("project_cidr_block") or "192.168.248.0/21",
    vpc_peering_connection_id=atlas_aws_network_peer.connection_id,
)

if atlas_config.get_bool("ready_for_traffic"):
    consul.Keys(
        "set-mongo-connection-info-in-consul",
        keys=[
            consul.KeysKeyArgs(
                path="mongodb/host",
                delete=True,
                value=atlas_cluster.mongo_uri.apply(lambda uri: urlparse(uri).netloc),
            ),
            consul.KeysKeyArgs(
                path="mongodb/use-ssl",
                delete=True,
                value=atlas_cluster.mongo_uri_with_options.apply(
                    lambda uri: parse_qs(urlparse(uri).query)["ssl"][0]
                ),
            ),
            consul.KeysKeyArgs(
                path="mongodb/replica-set",
                delete=True,
                value=atlas_cluster.mongo_uri_with_options.apply(
                    lambda uri: parse_qs(urlparse(uri).query)["replicaSet"][0]
                ),
            ),
            consul.KeysKeyArgs(path="mongodb/auth-source", delete=True, value="admin"),
        ],
        opts=pulumi.ResourceOptions(
            provider=consul.Provider(
                "consul-provider",
                address=pulumi.Config("consul").require("address"),
                scheme="https",
                http_auth="pulumi:{}".format(
                    read_yaml_secrets(
                        Path(f"pulumi/consul.{stack_info.env_suffix}.yaml")
                    )["basic_auth_password"]
                ),
            )
        ),
    )

########################
# Data Pipeline Access #
########################
atlas_data_security_group = aws.ec2.SecurityGroup(
    f"mongodb-atlas-data {stack_info.env_suffix}",
    name=f"mongodb-atlas-data {stack_info.env_suffix}",
    description=f"Access control to Mongodb Atlas instances in data {stack_info.env_suffix}",  # noqa: E501
    tags=aws_config.merged_tags(
        {"Name": f"data {stack_info.env_suffix}-mongodb-atlas"}
    ),
    vpc_id=target_vpc["id"],
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_MONGODB_PORT,
            to_port=DEFAULT_MONGODB_PORT,
            cidr_blocks=[target_vpc["cidr"]],
            ipv6_cidr_blocks=[target_vpc["cidr_v6"]],
            description=f"Access to Mongodb cluster from data {stack_info.env_suffix}",
        ),
    ],
    egress=default_egress_args,
)

# It is necessary to manually go to the Mongo Atlas UI and fetch the IP CIDR for adding
# to the VPC route table
atlas_aws_data_network_peer = atlas.NetworkPeering(
    f"mongo-atlas-network-peering-data-{stack_info.env_suffix}",
    accepter_region_name=data_vpc["region"],
    container_id=atlas_cluster.container_id,
    vpc_id=data_vpc["id"],
    aws_account_id=aws.get_caller_identity().account_id,
    project_id=atlas_project.id,
    provider_name="AWS",
    route_table_cidr_block=data_vpc["cidr"],
    opts=atlas_provider,
)

accept_atlas_data_network_peer = aws.ec2.VpcPeeringConnectionAccepter(
    "mongo-atlas-data-peering-connection-accepter",
    vpc_peering_connection_id=atlas_aws_network_peer.connection_id,
    auto_accept=True,
    accepter=aws.ec2.VpcPeeringConnectionAccepterAccepterArgs(
        allow_remote_vpc_dns_resolution=True,
    ),
    tags=aws_config.tags,
)

atlas_data_secgroup_network_access = atlas.ProjectIpAccessList(
    "mongo-atlas-data-network-security-group-permissions",
    aws_security_group=atlas_security_group.id,
    project_id=atlas_project.id,
    opts=pulumi.ResourceOptions(
        depends_on=[atlas_aws_network_peer, accept_atlas_network_peer]
    ).merge(atlas_provider),
)

atlas_data_cidr_network_access = atlas.ProjectIpAccessList(
    "mongo-atlas-data-network-cidr-block-permissions",
    project_id=atlas_project.id,
    cidr_block=data_vpc["cidr"],
    opts=pulumi.ResourceOptions(
        depends_on=[atlas_aws_network_peer, accept_atlas_network_peer]
    ).merge(atlas_provider),
)

aws.ec2.Route(
    "mongo-atlas-data-network-route",
    route_table_id=data_vpc["route_table_id"],
    destination_cidr_block=atlas_config.get("project_cidr_block") or "192.168.248.0/21",
    vpc_peering_connection_id=atlas_aws_data_network_peer.connection_id,
)

consul.Keys(
    "set-mongo-connection-info-in-consul-for-data-pipelines",
    keys=[
        consul.KeysKeyArgs(
            path="mongodb/host",
            delete=True,
            value=atlas_cluster.mongo_uri.apply(lambda uri: urlparse(uri).netloc),
        ),
        consul.KeysKeyArgs(
            path="mongodb/use-ssl",
            delete=True,
            value=atlas_cluster.mongo_uri_with_options.apply(
                lambda uri: parse_qs(urlparse(uri).query)["ssl"][0]
            ),
        ),
        consul.KeysKeyArgs(
            path="mongodb/replica-set",
            delete=True,
            value=atlas_cluster.mongo_uri_with_options.apply(
                lambda uri: parse_qs(urlparse(uri).query)["replicaSet"][0]
            ),
        ),
        consul.KeysKeyArgs(path="mongodb/auth-source", delete=True, value="admin"),
    ],
    opts=pulumi.ResourceOptions(
        provider=consul.Provider(
            "consul-operations-provider",
            # Writing to the operations Consul so that Salt can template the values
            address=f"https://consul-operations-{stack_info.env_suffix}.odl.mit.edu",
            scheme="https",
            http_auth="pulumi:{}".format(
                read_yaml_secrets(Path(f"pulumi/consul.{stack_info.env_suffix}.yaml"))[
                    "basic_auth_password"
                ]
            ),
        )
    ),
)


pulumi.export(
    "atlas_cluster",
    {
        "id": atlas_cluster.cluster_id,
        "mongo_uri": atlas_cluster.mongo_uri,
        "mongo_uri_with_options": atlas_cluster.mongo_uri_with_options,
        "connection_strings": atlas_cluster.connection_strings,
        "srv_record": atlas_cluster.srv_address,
    },
)

pulumi.export("project", {"id": atlas_project.id})
