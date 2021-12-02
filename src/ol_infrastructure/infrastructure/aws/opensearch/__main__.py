import json
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_consul as consul

from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.aws.ec2_helper import default_egress_args
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

###############
# STACK SETUP #
###############
search_config = pulumi.Config("opensearch")
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
cluster_size = search_config.get_int("cluster_size") or 3
cluster_instance_type = search_config.get("instance_type") or "t3.medium.elasticsearch"
disk_size = search_config.get_int("disk_size_gb") or 30  # noqa: WPS432

search_security_group = aws.ec2.SecurityGroup(
    "opensearch-security-group",
    name_prefix=f"{environment_name}-opensearch-",
    tags=aws_config.merged_tags({"Name": f"{environment_name}-opensearch"}),
    description="Grant access to the OpenSearch service domain",
    egress=default_egress_args,
    ingress=[
        aws.ec2.SecurityGroupIngressArgs(
            from_port=DEFAULT_HTTPS_PORT,
            to_port=DEFAULT_HTTPS_PORT,
            cidr_blocks=[target_vpc["cidr"]],
            protocol="tcp",
        )
    ],
    vpc_id=target_vpc["id"],
)

search_domain = aws.elasticsearch.Domain(
    "opensearch-domain-cluster",
    domain_name=f"opensearch-{environment_name}",
    elasticsearch_version=search_config.get("engine_version") or "7.10",
    cluster_config=aws.elasticsearch.DomainClusterConfigArgs(
        zone_awareness_enabled=True,
        zone_awareness_config=aws.elasticsearch.DomainClusterConfigZoneAwarenessConfigArgs(  # noqa: E501
            availability_zone_count=3
        ),
        instance_count=cluster_size,
        instance_type=cluster_instance_type,
    ),
    vpc_options=aws.elasticsearch.DomainVpcOptionsArgs(
        subnet_ids=target_vpc["subnet_ids"][:3],
        security_group_ids=[search_security_group.id],
    ),
    ebs_options=aws.elasticsearch.DomainEbsOptionsArgs(
        ebs_enabled=True,
        volume_type="gp2",
        volume_size=disk_size,
    ),
    tags=aws_config.merged_tags({"Name": f"{environment_name}-opensearch-cluster"}),
)

search_domain_policy = aws.elasticsearch.DomainPolicy(
    "opensearch-domain-cluster-access-policy",
    domain_name=search_domain.domain_name,
    access_policies=search_domain.arn.apply(
        lambda arn: json.dumps(
            {
                "Version": IAM_POLICY_VERSION,
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"AWS": "*"},
                        "Action": "es:*",
                        "Resource": f"{arn}/*",
                    }
                ],
            }
        )
    ),
)

consul_config = pulumi.Config("consul")
consul_provider = consul.Provider(
    "consul-provider",
    address=consul_config.require("address"),
    scheme="https",
    http_auth="pulumi:{}".format(
        read_yaml_secrets(Path(f"pulumi/consul.{stack_info.env_suffix}.yaml"))[
            "basic_auth_password"
        ]
    ),
)

opensearch_node = consul.Node(
    "aws-opensearch-consul-node",
    address=search_domain.endpoint,
    opts=pulumi.ResourceOptions(provider=consul_provider),
)

opensearch_service = consul.Service(
    "aws-opensearch-consul-service",
    node=opensearch_node.name,
    name="elasticsearch",
    port=DEFAULT_HTTPS_PORT,
    meta={
        "external-node": True,
        "external-probe": True,
    },
    checks=[
        consul.ServiceCheckArgs(
            check_id="elasticsearch",
            interval="10s",
            name="elasticsearch",
            timeout="1m0s",
            status="passing",
            tcp=pulumi.Output.all(
                address=search_domain.endpoint, port=DEFAULT_HTTPS_PORT
            ).apply(
                lambda es: "{address}:{port}".format(**es)
            ),  # noqa: WPS237,E501
        )
    ],
    opts=pulumi.ResourceOptions(provider=consul_provider),
)

pulumi.export(
    "cluster",
    {
        "endpoint": search_domain.endpoint,
        "arn": search_domain.arn,
        "domain_name": search_domain.domain_name,
        "domain_id": search_domain.domain_id,
        "urn": search_domain.urn,
    },
)

pulumi.export("security_group", search_security_group.id)
