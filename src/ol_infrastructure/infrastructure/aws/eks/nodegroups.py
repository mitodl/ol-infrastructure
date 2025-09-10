import hashlib
import json
from typing import Any

import pulumi_aws as aws
import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, ResourceOptions, export

from ol_infrastructure.lib.aws.eks_helper import get_cluster_version
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack


def create_and_update_nodegroup(  # noqa: PLR0913
    ng_name: str,
    ng_config: dict[str, Any],
    cluster: eks.Cluster,
    cluster_role: aws.iam.Role,
    node_role: aws.iam.Role,
    node_instance_profile: aws.iam.InstanceProfile,
    target_vpc: Output[dict[str, Any]],
    aws_config: AWSBase,
    k8s_provider: kubernetes.Provider,
):
    node_groups = []
    stack_info = parse_stack()
    cluster_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
    eks_config = Config("eks")
    pod_ip_blocks = target_vpc["k8s_pod_subnet_cidrs"]
    taint_list = {}
    for taint_name, taint_config in ng_config["taints"].items() or {}:
        taint_list[taint_name] = eks.TaintArgs(
            value=taint_config["value"],
            effect=taint_config["effect"],
        )

    # New hash-based blue/green logic:
    # 1. Always have exactly one active nodegroup hash recorded in SSM (active_hash).
    # 2. When desired hash differs, create candidate nodegroup alongside active; keep
    # active untouched.
    # 3. Exports expose active & candidate ASG names for drain operations.
    # 4. When promotion flag set and candidate hash matches desired, only create new
    # active and remove old.
    architecture = ng_config.get("architecture") or "x86_64"
    ami_type = "nvidia" if ng_config.get("gpu") else "standard"
    ami_param = f"/aws/service/eks/optimized-ami/{get_cluster_version()}/amazon-linux-2023/{architecture}/{ami_type}/recommended/image_id"  # noqa: E501
    resolved_ami = aws.ssm.get_parameter(name=ami_param).value
    desired_bytes = json.dumps(
        {
            "cluster_version": get_cluster_version(),
            "instance_type": ng_config["instance_type"],
            "gpu": ng_config.get("gpu") or False,
            "disk": ng_config["disk_size_gb"] or 250,
            "labels": ng_config["labels"] or {},
            "taints": ng_config.get("taints") or {},
            "ami_id": resolved_ami,
        },
        sort_keys=True,
    ).encode()
    desired_hash = hashlib.sha1(desired_bytes, usedforsecurity=False).hexdigest()[:8]

    active_param_name = (
        f"/ol-infrastructure/eks/{cluster_name}/nodegroups/{ng_name}/active_hash"
    )
    candidate_param_name = (
        f"/ol-infrastructure/eks/{cluster_name}/nodegroups/{ng_name}/candidate_hash"
    )

    def _ssm_get(name: str) -> str | None:
        try:
            return aws.ssm.get_parameter(name=name).value
        except Exception:  # noqa: BLE001
            return None

    active_hash = _ssm_get(active_param_name)
    candidate_hash = _ssm_get(candidate_param_name)
    promote = eks_config.get_bool("promote_nodegroups") or False

    # Bootstrap active hash if absent
    if not active_hash:
        active_hash = desired_hash
        aws.ssm.Parameter(
            f"{cluster_name}-eks-nodegroup-{ng_name}-active-hash-param",
            name=active_param_name,
            type="String",
            value=active_hash,
            overwrite=True,
            tags=aws_config.tags,
        )

    need_candidate = desired_hash not in [active_hash, candidate_hash]
    promoting = promote and candidate_hash and candidate_hash == desired_hash

    if need_candidate:
        candidate_hash = desired_hash
        aws.ssm.Parameter(
            f"{cluster_name}-eks-nodegroup-{ng_name}-candidate-hash-param",
            name=candidate_param_name,
            type="String",
            value=candidate_hash,
            overwrite=True,
            tags=aws_config.tags,
        )

    if promoting:
        # Update active hash to candidate and do not recreate candidate
        aws.ssm.Parameter(
            f"{cluster_name}-eks-nodegroup-{ng_name}-active-hash-promote",
            name=active_param_name,
            type="String",
            value=candidate_hash,
            overwrite=True,
            tags=aws_config.tags,
        )
        active_hash = candidate_hash
        candidate_hash = None  # stop defining candidate resource

    # Helper for nodegroup creation
    def _create_ng(hash_value: str, export_key: str, *, is_active: bool = True):
        resource_options = ResourceOptions(parent=cluster, depends_on=cluster)
        if is_active:
            resource_options.merge(
                ResourceOptions(ignore_changes=["instance_type", "image_id"])
            )
        sec = eks.NodeGroupSecurityGroup(
            f"{cluster_name}-eks-nodegroup-{ng_name}-{hash_value}-secgroup",
            cluster_security_group=cluster.cluster_security_group,
            eks_cluster=cluster.eks_cluster,
            vpc_id=target_vpc["id"],
            tags=aws_config.tags,
        )
        ng_res = eks.NodeGroupV2(
            f"{cluster_name}-eks-nodegroup-{ng_name}-{hash_value}",
            cluster=eks.CoreDataArgs(
                cluster=cluster.eks_cluster,
                cluster_iam_role=cluster_role,
                endpoint=cluster.eks_cluster.endpoint,
                instance_roles=[node_role],
                node_group_options=eks.ClusterNodeGroupOptionsArgs(
                    node_associate_public_ip_address=False,
                ),
                subnet_ids=target_vpc["k8s_pod_subnet_ids"],
                vpc_id=target_vpc["id"],
                provider=k8s_provider,
            ),
            launch_template_tag_specifications=[
                aws.ec2.LaunchTemplateTagSpecificationArgs(
                    resource_type="instance",
                    tags=aws_config.merged_tags(ng_config.get("tags") or {}),
                ),
                aws.ec2.LaunchTemplateTagSpecificationArgs(
                    resource_type="volume",
                    tags=aws_config.merged_tags(ng_config.get("tags") or {}),
                ),
            ],
            gpu=ng_config.get("gpu") or False,
            min_refresh_percentage=eks_config.get_int("min_refresh_percentage") or 90,
            instance_type=ng_config["instance_type"],
            instance_profile=node_instance_profile,
            labels=ng_config.get("labels", {}),
            node_security_group=sec.security_group,
            node_root_volume_size=ng_config["disk_size_gb"] or 250,
            node_root_volume_delete_on_termination=True,
            node_root_volume_type="gp3",
            cluster_ingress_rule=sec.security_group_rule,
            desired_capacity=ng_config["scaling"]["desired"] or 3,
            max_size=ng_config["scaling"]["max"] or 5,
            min_size=ng_config["scaling"]["min"] or 2,
            taints=taint_list,
            opts=resource_options,
        )
        aws.ec2.SecurityGroupRule(
            f"{cluster_name}-eks-nodegroup-{ng_name}-{hash_value}-podcidr",
            type="ingress",
            description="Allow all traffic from pod CIDRs",
            security_group_id=sec.security_group.id,
            protocol="-1",
            from_port=0,
            to_port=0,
            cidr_blocks=pod_ip_blocks,
        )
        export(export_key, ng_res.auto_scaling_group.name)
        return ng_res

    # active_hash guaranteed non-None after bootstrap above
    active_ng = _create_ng(active_hash, f"{ng_name}_active_asg_name", is_active=True)  # type: ignore[arg-type]
    node_groups.append(active_ng)
    if candidate_hash:
        candidate_ng = _create_ng(
            candidate_hash, f"{ng_name}_candidate_asg_name", is_active=False
        )
        node_groups.append(candidate_ng)
        # Bi-directional SG rules
        aws.ec2.SecurityGroupRule(
            f"{cluster_name}-eks-nodegroup-{ng_name}-{active_hash}-to-{candidate_hash}",
            type="ingress",
            security_group_id=active_ng.node_security_group_id,
            source_security_group_id=candidate_ng.node_security_group_id,
            protocol="-1",
            from_port=0,
            to_port=0,
        )
        aws.ec2.SecurityGroupRule(
            f"{cluster_name}-eks-nodegroup-{ng_name}-{candidate_hash}-to-{active_hash}",
            type="ingress",
            security_group_id=candidate_ng.node_security_group_id,
            source_security_group_id=active_ng.node_security_group_id,
            protocol="-1",
            from_port=0,
            to_port=0,
        )
    return node_groups
