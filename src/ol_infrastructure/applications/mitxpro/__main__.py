from pulumi import StackReference, export, get_stack
from pulumi.config import get_config
from pulumi_aws import ec2
from pulumi_consul import Node, Service

from ol_infrastructure.components.aws.database import OLAmazonDB, OLMariaDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultMariaDatabaseConfig,
)
from ol_infrastructure.infrastructure.operations.consul import (
    consul_server_security_group,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.stack_defaults import defaults

stack = get_stack()
stack_name = stack.split(".")[-1]
namespace = stack.rsplit(".", 1)[0]
env_suffix = stack_name.lower()
network_stack = StackReference(f"infrastructure.aws.network.{stack_name}")
dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
mitxpro_vpc = network_stack.require_output("mitxpro_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
mitxpro_environment = f"mitxpro-{env_suffix}"
aws_config = AWSBase(
    tags={"OU": "mitxpro", "Environment": mitxpro_environment},
)

mitxpro_edxapp_security_group = ec2.SecurityGroup(
    f"mitxpro-edxapp-access-{env_suffix}",
    name=f"mitxpro-edxapp-access-{env_suffix}",
    description="Access control to mitxpro_edxapp",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[mitxpro_vpc["cidr"]],
            ipv6_cidr_blocks=[mitxpro_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=22,
            to_port=22,
            description="mitxpro_vpc ssh access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[operations_vpc["cidr"]],
            ipv6_cidr_blocks=[operations_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=22,
            to_port=22,
            description="operations_vpc ssh access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            protocol="tcp",
            from_port=80,
            to_port=80,
            description="HTTP access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            protocol="tcp",
            from_port=443,
            to_port=443,
            description="HTTPS access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[mitxpro_vpc["cidr"]],
            ipv6_cidr_blocks=[mitxpro_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=18040,
            to_port=18040,
            description="Xqueue access",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=mitxpro_vpc["id"],
)

mitxpro_edxapp_security_group = ec2.SecurityGroup(
    f"mitxpro-edxapp-access-{env_suffix}",
    name=f"mitxpro-edxapp-access-{env_suffix}",
    description="Access control to mitxpro_edxapp",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[mitxpro_vpc["cidr"]],
            ipv6_cidr_blocks=[mitxpro_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=22,
            to_port=22,
            description="mitxpro_vpc ssh access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[operations_vpc["cidr"]],
            ipv6_cidr_blocks=[operations_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=22,
            to_port=22,
            description="operations_vpc ssh access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            protocol="tcp",
            from_port=80,
            to_port=80,
            description="HTTP access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            protocol="tcp",
            from_port=443,
            to_port=443,
            description="HTTPS access",
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[mitxpro_vpc["cidr"]],
            ipv6_cidr_blocks=[mitxpro_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=18040,
            to_port=18040,
            description="Xqueue access",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=mitxpro_vpc["id"],
)

mitxpro_edx_worker_security_group = ec2.SecurityGroup(
    f"mitxpro-edx_worker-access-{env_suffix}",
    name=f"mitxpro-edx_worker-access-{env_suffix}",
    description="Access control to mitxpro_edx_worker",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[mitxpro_edxapp_security_group.id],
            protocol="tcp",
            from_port=18040,  # noqa: WPS432
            to_port=18040,  # noqa: WPS432
            description="Xqueue access",
        )
    ],
    tags=aws_config.tags,
    vpc_id=mitxpro_vpc["id"],
)

mitxpro_edxapp_db_security_group = ec2.SecurityGroup(
    f"mitxpro-edxapp-db-access-{env_suffix}",
    name=f"mitxpro-edxapp-db-access-{env_suffix}",
    description="Access from the mitxpro VPC to the mitxpro edxapp database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                mitxpro_edxapp_security_group.id,
                mitxpro_edx_worker_security_group.id,
                consul_server_security_group.id,
            ],
            protocol="tcp",
            from_port=3306,  # noqa: WPS432
            to_port=3306,  # noqa: WPS432
        )
    ],
    tags=aws_config.tags,
    vpc_id=mitxpro_vpc["id"],
)

mitxpro_edxapp_db_config = OLMariaDBConfig(
    instance_name=f"ol-mitxpro-edxapp-db-{env_suffix}",
    password=get_config("mitxpro_edxapp:db_password"),
    subnet_group_name=mitxpro_vpc["rds_subnet"],
    security_groups=[mitxpro_edxapp_db_security_group],
    tags=aws_config.tags,
    db_name="edxapp_{db_purpose}".format(
        db_purpose=(get_config("mitxpro_edxapp:db_purpose"))
    ),
    **defaults(stack)["rds"],
)
mitxpro_edxapp_db = OLAmazonDB(mitxpro_edxapp_db_config)

mitxpro_edxapp_db_vault_backend_config = OLVaultMariaDatabaseConfig(
    db_name=mitxpro_edxapp_db_config.db_name,
    mount_point=f"{mitxpro_edxapp_db_config.engine}-mitxpro-edxapp-{mitxpro_environment}",
    db_admin_username=mitxpro_edxapp_db_config.username,
    db_admin_password=get_config("mitxpro_edxapp:db_password"),
    db_host=mitxpro_edxapp_db.db_instance.address,
)
mitxpro_edxapp_db_vault_backend = OLVaultDatabaseBackend(
    mitxpro_edxapp_db_vault_backend_config
)

mitxpro_edxapp_db_consul_node = Node(
    "mysql", name="mysql", address=mitxpro_edxapp_db.db_instance.address
)

mitxpro_edxapp_db_consul_service = Service(
    "mysql",
    node="mysql",
    name="mysql",
    port=3306,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        {
            "check_id": "mitxpro_edxapp_db",
            "interval": "10s",
            "name": "mitxpro_edxapp_db",
            "timeout": "60s",
            "status": "passing",
        }
    ],
    tags=["rds", "mitxpro", "mitxpro_edxapp", mitxpro_environment],
)

export(
    "mitxpro_edxapp",
    {"rds_host": mitxpro_edxapp_db.db_instance.address},
)
