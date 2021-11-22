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
vault_stack = pulumi.StackReference(
    f"infrastructure.vault.operations.{stack_info.name}"
)

#############
# VARIABLES #
#############
environment_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc = network_stack.require_output(env_config.require("target_vpc"))
business_unit = env_config.get("business_unit") or "operations"
aws_config = AWSBase(tags={"OU": business_unit, "Environment": environment_name})
max_disk_size = atlas_config.get("disk_autoscale_max_gb")
max_instance_type = atlas_config.get("cluster_autoscale_max_size")
min_instance_type = atlas_config.get("cluster_autoscale_min_size")
vault_server = vault_stack.require_output("vault_server")

#################
# ATLAS PROJECT #
#################
atlas_project = atlas.Project(
    f"mongo-atlas-project-{environment_name}",
    name=environment_name,
    org_id=atlas_config.require("organization_id"),
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
        aws.ec2.SecurityGroupIngressArgs(
            security_groups=[vault_server["security_group"]],
            protocol="tcp",
            from_port=DEFAULT_MONGODB_PORT,
            to_port=DEFAULT_MONGODB_PORT,
            description="Access to Mongodb cluster from Vault",
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
)

accept_atlas_network_peer = aws.ec2.VpcPeeringConnectionAccepter(
    "mongo-atlas-peering-connection-accepter",
    vpc_peering_connection_id=atlas_aws_network_peer.connection_id,
    auto_accept=True,
    tags=aws_config.tags,
)

atlas_network_access = atlas.ProjectIpAccessList(
    "mongo-atlas-network-permissions",
    aws_security_group=atlas_security_group.id,
    project_id=atlas_project.id,
    opts=pulumi.ResourceOptions(
        depends_on=[atlas_aws_network_peer, accept_atlas_network_peer]
    ),
)

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
    ],
    opts=pulumi.ResourceOptions(
        provider=consul.Provider(
            "consul-provider",
            address=pulumi.Config("consul").require("address"),
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
