from pulumi import Config, StackReference, export, get_stack
from pulumi_aws import ec2
from pulumi_consul import Node, Service

from ol_infrastructure.components.aws.database import OLAmazonDB, OLMariaDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultMysqlDatabaseConfig,
)
from ol_infrastructure.lib.ol_types import Apps, AWSBase
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import mysql_sql_statements

stack = get_stack()
stack_name = stack.split(".")[-1]
namespace = stack.rsplit(".", 1)[0]
env_suffix = stack_name.lower()
network_stack = StackReference(f"infrastructure.aws.network.{stack_name}")
operations_stack = StackReference(f"infrastructure.operations.xpro.{stack_name}")
dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")
xpro_vpc = network_stack.require_output("xpro_vpc")
operations_vpc = network_stack.require_output("operations_vpc")
mitxpro_environment = f"mitxpro-{env_suffix}"
mitxpro_config = Config("mitxpro_edxapp")
aws_config = AWSBase(
    tags={
        "OU": "mitxpro",
        "Environment": mitxpro_environment,
        "application": Apps.mitxpro_edx.value,
    },
)

mitxpro_edxapp_security_group = ec2.SecurityGroup(
    f"mitxpro-edxapp-access-{env_suffix}",
    name=f"mitxpro-edxapp-access-{env_suffix}",
    description="Access control to mitxpro_edxapp",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[xpro_vpc["cidr"]],
            ipv6_cidr_blocks=[xpro_vpc["cidr_v6"]],
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
            cidr_blocks=[xpro_vpc["cidr"]],
            ipv6_cidr_blocks=[xpro_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=18040,
            to_port=18040,
            description="Xqueue access",
        ),
    ],
    tags=aws_config.tags,
    vpc_id=xpro_vpc["id"],
)

mitxpro_edx_worker_security_group = ec2.SecurityGroup(
    f"mitxpro-edx-worker-access-{env_suffix}",
    name=f"mitxpro-edx-worker-access-{env_suffix}",
    description="Access control to mitxpro_edx_worker",
    tags=aws_config.tags,
    vpc_id=xpro_vpc["id"],
)

mitxpro_edxapp_db_security_group = ec2.SecurityGroup(
    f"mitxpro-edxapp-db-access-{env_suffix}",
    name=f"mitxpro-edxapp-db-access-{env_suffix}",
    description="Access from the mitxpro and operations VPC to the mitxpro edxapp database",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            security_groups=[
                mitxpro_edxapp_security_group.id,
                mitxpro_edx_worker_security_group.id,
            ],
            protocol="tcp",
            from_port=3306,
            to_port=3306,
        ),
        ec2.SecurityGroupIngressArgs(
            cidr_blocks=[operations_vpc["cidr"]],
            ipv6_cidr_blocks=[operations_vpc["cidr_v6"]],
            protocol="tcp",
            from_port=3306,
            to_port=3306,
        ),
    ],
    tags=aws_config.tags,
    vpc_id=xpro_vpc["id"],
)

mitxpro_edxapp_db_config = OLMariaDBConfig(
    instance_name=f"ol-mitxpro-edxapp-db-{env_suffix}",
    password=mitxpro_config.require("db_password"),
    subnet_group_name=xpro_vpc["rds_subnet"],
    security_groups=[mitxpro_edxapp_db_security_group],
    tags=aws_config.merged_tags({"Name": f"mitxpro-edxapp-{env_suffix}-db"}),
    db_name="edxapp_{db_purpose}".format(
        db_purpose=mitxpro_config.require("db_purpose")
    ),
    **defaults(stack)["rds"],
)
mitxpro_edxapp_db = OLAmazonDB(mitxpro_edxapp_db_config)

hyphenated_db_purpose = (mitxpro_config.require("db_purpose")).replace("_", "-")

edx_role_statments = mysql_sql_statements.update(
    {
        f"edxapp-csmh-{hyphenated_db_purpose}": {
            "create": "CREATE USER '{{{{name}}}}'@'%' IDENTIFIED BY '{{{{password}}}}';"
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES"  # noqa: Q000
            "CREATE TEMPORARY TABLES, LOCK TABLES ON edxapp_csmh_{mitxpro_config.require('db_purpose')}.* TO '{{{{name}}}}'@'%';",
            "revoke": "DROP USER '{{{{name}}}}';",
        },
        f"edxapp-{hyphenated_db_purpose}": {
            "create": "CREATE USER '{{{{name}}}}'@'%' IDENTIFIED BY '{{{{password}}}}';"
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES"  # noqa: Q000
            "CREATE TEMPORARY TABLES, LOCK TABLES ON edxapp_{mitxpro_config.require('db_purpose')}.* TO '{{{{name}}}}'@'%';",
            "revoke": "DROP USER '{{{{name}}}}';",
        },
        f"xqueue-{hyphenated_db_purpose}": {
            "create": "CREATE USER '{{{{name}}}}'@'%' IDENTIFIED BY '{{{{password}}}}';"
            "GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, INDEX, DROP, ALTER, REFERENCES"  # noqa: Q000
            "CREATE TEMPORARY TABLES, LOCK TABLES ON xqueue_{mitxpro_config.require('db_purpose')}.* TO '{{{{name}}}}'@'%';",
            "revoke": "DROP USER '{{{{name}}}}';",
        },
    }
)

mitxpro_edxapp_db_vault_backend_config = OLVaultMysqlDatabaseConfig(
    db_name=mitxpro_edxapp_db_config.db_name,
    mount_point=f"{mitxpro_edxapp_db_config.engine}-mitxpro-edxapp-{mitxpro_environment}",
    db_admin_username=mitxpro_edxapp_db_config.username,
    db_admin_password=mitxpro_config.require("db_password"),
    db_host=mitxpro_edxapp_db.db_instance.address,
    role_statements=mysql_sql_statements,
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
    # TODO Add checks as an instance of ServiceCheckArgs to allow for type enforcement
    checks=[
        {
            "check_id": "mitxpro_edxapp_db",
            "interval": "10s",
            "name": "mitxpro_edxapp_db",
            "timeout": "60s",
            "status": "passing",
            "tcp": f"{mitxpro_edxapp_db.db_instance.address}:3306",
        }
    ],
    tags=["rds", "mitxpro", "mitxpro_edxapp", mitxpro_environment],
)

export(
    "mitxpro_edxapp",
    {
        "rds_host": mitxpro_edxapp_db.db_instance.address,
        "mitxpro_edxapp_security_group": mitxpro_edxapp_security_group.id,
        "mitxpro_edx_worker_security_group": mitxpro_edx_worker_security_group.id,
        "mitxpro_edxapp_db_security_group": mitxpro_edxapp_db_security_group.id,
    },
)
