# ruff: noqa: E501
import json
import re

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pulumi import Output, ResourceOptions, StackReference

from bridge.lib.magic_numbers import AWS_EVENT_TARGET_GROUP_NAME_MAX_LENGTH
from bridge.lib.versions import KARPENTER_CHART_VERSION
from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.lib.aws.ec2_helper import InstanceClasses, InstanceTypes
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.substructure.aws.eks.karpenter_iam import (
    get_cluster_karpenter_iam_policy_document,
)


def extract_version(ami_name: str) -> str:
    """Extract the version suffix (e.g., vYYYYMMDD) from an AMI name."""
    match = re.search(r"(v\d+)$", ami_name)
    if match:
        return match.group(1)
    else:
        # Fallback or error handling if pattern doesn't match
        # Using 'latest' might cause unintended upgrades; raising an error might be safer in prod.
        # For now, log a warning and use latest as per Karpenter docs example.
        pulumi.log.warn(
            f"Could not extract version from AMI name: {ami_name}. Defaulting to 'latest'."
        )
        return "latest"


def setup_karpenter(  # noqa: PLR0913
    cluster_name: str,
    cluster_stack: StackReference,
    kms_stack: StackReference,
    aws_config: AWSBase,
    k8s_provider: kubernetes.Provider,
    aws_account: aws.GetCallerIdentityResult,
    k8s_global_labels: dict[str, str],
):
    """
    Set up Karpenter resources including SQS queue, EventBridge rules, IAM roles/policies,
    Helm chart installation, and default NodePool/EC2NodeClass.

    Args:
        cluster_name: The name of the EKS cluster.
        cluster_stack: A StackReference to the EKS cluster stack.
        aws_config: The AWS configuration object containing tags.
        k8s_provider: The Pulumi Kubernetes provider instance.
        aws_account: The AWS caller identity result.
        k8s_global_labels: A dictionary of global labels to apply to Kubernetes resources.
    """
    # Karpenter Interruption Queue
    karpenter_interruption_queue = aws.sqs.Queue(
        f"{cluster_name}-karpenter-interruption-queue",
        name=cluster_name,
        message_retention_seconds=300,
        sqs_managed_sse_enabled=True,
        tags=aws_config.merged_tags({"Name": cluster_name}),
    )

    aws.sqs.QueuePolicy(
        f"{cluster_name}-karpenter-interruption-queue-policy",
        queue_url=karpenter_interruption_queue.id,
        policy=Output.all(queue_arn=karpenter_interruption_queue.arn).apply(
            lambda args: json.dumps(
                {
                    "Version": IAM_POLICY_VERSION,
                    "Id": "EC2InterruptionPolicy",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Principal": {
                                "Service": [
                                    "events.amazonaws.com",
                                    "sqs.amazonaws.com",
                                ]
                            },
                            "Action": "sqs:SendMessage",
                            "Resource": args["queue_arn"],
                        },
                        {
                            "Sid": "DenyHTTP",
                            "Effect": "Deny",
                            "Principal": "*",
                            "Action": "sqs:*",
                            "Resource": args["queue_arn"],
                            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                        },
                    ],
                }
            )
        ),
    )

    # EventBridge Rules targeting the interruption queue
    event_patterns = {
        "scheduled-change": {
            "source": ["aws.health"],
            "detail-type": ["AWS Health Event"],
        },
        "spot-interruption": {
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Spot Instance Interruption Warning"],
        },
        "rebalance": {
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Instance Rebalance Recommendation"],
        },
        "instance-state-change": {
            "source": ["aws.ec2"],
            "detail-type": ["EC2 Instance State-change Notification"],
        },
    }

    for rule_name_suffix, event_pattern in event_patterns.items():
        rule = aws.cloudwatch.EventRule(
            f"{cluster_name}-karpenter-interruption-{rule_name_suffix}-rule",
            name=f"{cluster_name}-karpenter-{rule_name_suffix}"[
                :AWS_EVENT_TARGET_GROUP_NAME_MAX_LENGTH
            ],
            event_pattern=json.dumps(event_pattern),
            tags=aws_config.tags,
        )
        aws.cloudwatch.EventTarget(
            f"{cluster_name}-karpenter-interruption-{rule_name_suffix}-target",
            target_id=f"{cluster_name}-karpenter-interruption-{rule_name_suffix}"[
                :AWS_EVENT_TARGET_GROUP_NAME_MAX_LENGTH
            ],
            rule=rule.name,
            arn=karpenter_interruption_queue.arn,
        )

    # Karpenter Controller Trust Role (IAM Role for Service Account - IRSA)
    karpenter_trust_role = OLEKSTrustRole(
        f"{cluster_name}-karpenter-controller-trust-role",
        role_config=OLEKSTrustRoleConfig(
            account_id=aws_account.account_id,
            cluster_name=cluster_name,
            cluster_identities=cluster_stack.require_output("cluster_identities"),
            description="Trust role for allowing karpenter to create and destroy "
            "ec2 instances from within the cluster.",
            policy_operator="StringEquals",
            role_name="karpenter",
            service_account_identifier="system:serviceaccount:operations:karpenter",  # Matches the Helm chart default SA name
            tags=aws_config.tags,
        ),
        opts=ResourceOptions(),
    )

    # Generate and create the Karpenter Controller IAM Policy
    karpenter_controller_policy_document = Output.all(
        partition=aws.get_partition().partition,
        region=aws.get_region().name,
        account_id=aws_account.account_id,
        cluster_name=cluster_name,
        interruption_queue_arn=karpenter_interruption_queue.arn,
        node_role_arn=cluster_stack.require_output("node_role_arn"),
    ).apply(
        lambda args: get_cluster_karpenter_iam_policy_document(
            aws_partition=args["partition"],
            aws_region=args["region"],
            aws_account_id=args["account_id"],
            cluster_name=args["cluster_name"],
            karpenter_interruption_queue_arn=args["interruption_queue_arn"],
            karpenter_node_role_arn=args["node_role_arn"],
        )
    )

    karpenter_controller_policy = aws.iam.Policy(
        f"{cluster_name}-karpenter-controller-policy",
        name=f"KarpenterControllerPolicy-{cluster_name}",
        policy=karpenter_controller_policy_document.apply(json.dumps),
        tags=aws_config.tags,
    )

    # Attach the Controller Policy to the Trust Role
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-karpenter-controller-policy-attachment",
        role=karpenter_trust_role.role.name,
        policy_arn=karpenter_controller_policy.arn,
    )

    crd_info = {
        "ec2nodeclasses.karpenter.k8s.aws": "EC2NodeClass",
        "nodeclaims.karpenter.sh": "NodeClaim",
        "nodepools.karpenter.sh": "NodePool",
    }
    crd_patches = []
    for crd_name, kind in crd_info.items():
        group = crd_name.split(".", 1)[1]
        patch = kubernetes.apiextensions.v1.CustomResourceDefinitionPatch(
            f"patch-crd-{crd_name.replace('.', '-')}",
            metadata=kubernetes.meta.v1.ObjectMetaPatchArgs(
                name=crd_name,
                labels={"app.kubernetes.io/managed-by": "Helm"},
                annotations={
                    "meta.helm.sh/release-name": "karpeter-crds",
                    "meta.helm.sh/release-namespace": "operations",
                },
            ),
            spec=kubernetes.apiextensions.v1.CustomResourceDefinitionSpecPatchArgs(
                group=group,
                names=kubernetes.apiextensions.v1.CustomResourceDefinitionNamesPatchArgs(
                    kind=kind
                ),
            ),
            opts=ResourceOptions(provider=k8s_provider),
        )
        crd_patches.append(patch)

    # Install Karpeter CRD Helm Chart
    karpenter_crd_release = kubernetes.helm.v3.Release(
        f"{cluster_name}-karpeter-crd-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="karpeter-crds",
            chart="oci://public.ecr.aws/karpenter/karpenter-crd",
            version=KARPENTER_CHART_VERSION,
            disable_crd_hooks=True,
            namespace="operations",
            cleanup_on_fail=True,
            skip_await=False,
            values={},
        ),
        opts=ResourceOptions(provider=k8s_provider, depends_on=crd_patches),
    )
    # Install Karpenter Helm Chart
    karpenter_release = kubernetes.helm.v3.Release(
        f"{cluster_name}-karpenter-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="karpenter",
            chart="oci://public.ecr.aws/karpenter/karpenter",
            version=KARPENTER_CHART_VERSION,
            namespace="operations",  # Deploy into the operations namespace
            cleanup_on_fail=True,
            skip_await=False,  # Wait for resources to be ready
            skip_crds=True,  # CRDs are managed by the separate CRD cha
            values={
                # Configure IRSA
                "serviceAccount": {
                    "create": True,  # Let the chart create the SA
                    "name": "karpenter",
                    "annotations": {
                        "eks.amazonaws.com/role-arn": karpenter_trust_role.role.arn,
                    },
                },
                "serviceMonitor": {
                    "enabled": False,  # works TOO well, need to find a good way to reduce # of metrics
                },
                "controller": {
                    "resources": {
                        "requests": {
                            "cpu": "100m",
                            "memory": "256Mi",
                        },
                        "limits": {
                            "cpu": "200m",
                            "memory": "512Mi",
                        },
                    },
                },
                "settings": {
                    # Use cluster name and endpoint from the EKS stack output
                    "clusterName": cluster_name,
                    "clusterEndpoint": cluster_stack.require_output("kube_config_data")[
                        "server"
                    ],
                    # Configure interruption handling
                    "interruptionQueue": karpenter_interruption_queue.name,
                    "featureGates": {"spotToSpotConsolidation": True},
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[
                karpenter_crd_release,  # Ensure CRDs are installed first
                karpenter_trust_role,
                karpenter_controller_policy,  # Ensure policy exists before Helm install
                karpenter_interruption_queue,  # Ensure queue exists
            ],
            delete_before_replace=True,  # Useful for Helm upgrades/changes
        ),
    )

    # --- Dynamically determine the EKS Optimized AL2023 AMI Alias ---
    # Get cluster version from the referenced stack
    cluster_version = cluster_stack.require_output("cluster_version")

    # Construct the SSM parameter name dynamically
    ssm_parameter_name = cluster_version.apply(
        lambda version: f"/aws/service/eks/optimized-ami/{version}/amazon-linux-2023/x86_64/standard/recommended/image_id"
    )

    # Get the recommended AMI ID from SSM Parameter Store as an Output
    recommended_ami_id_output = aws.ssm.get_parameter_output(
        name=ssm_parameter_name,
    ).apply(lambda param_result: param_result.value)  # Apply to get Output[str]

    # Get the AMI details using the recommended AMI ID (which is an Output[str])
    # We need to call get_ami within the apply block
    recommended_ami_output = recommended_ami_id_output.apply(
        lambda ami_id: aws.ec2.get_ami(  # Call get_ami inside apply
            filters=[
                aws.ec2.GetAmiFilterArgs(
                    name="image-id",
                    values=[ami_id],  # Use the resolved ami_id here
                ),
            ],
            owners=["amazon"],  # EKS optimized AMIs are owned by Amazon
            most_recent=True,
        )
    )

    ami_version_string = recommended_ami_output.apply(
        lambda ami: extract_version(ami.name)
    )

    # Construct the alias using the extracted version
    ami_alias = ami_version_string.apply(lambda version: f"al2023@{version}")
    # --- End AMI Alias Determination ---

    # Get KMS key ARN for EBS encryption
    kms_ebs_id = kms_stack.require_output("kms_ec2_ebs_key")["id"]

    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-karpenter-default-node-class",
        api_version="karpenter.k8s.aws/v1",  # Correct API version for EC2NodeClass
        kind="EC2NodeClass",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="default",
            namespace="operations",
            labels=k8s_global_labels,
        ),
        spec={
            "kubelet": {},
            "blockDeviceMappings": [
                {
                    "deviceName": "/dev/xvda",
                    "ebs": {
                        "volumeSize": "1000Gi",
                        "volumeType": "gp3",
                        "iops": 3000,
                        "throughput": 125,
                        "deleteOnTermination": True,
                        "encrypted": True,
                        "kmsKeyID": kms_ebs_id,
                    },
                }
            ],
            "subnetSelectorTerms": cluster_stack.require_output("pod_subnet_ids").apply(
                lambda ids: [{"id": subnet_id} for subnet_id in ids]
            ),
            "securityGroupSelectorTerms": [
                {"id": cluster_stack.require_output("node_group_security_group_id")},
            ],
            "instanceProfile": cluster_stack.require_output("node_instance_profile"),
            # Dynamically select the EKS Optimized AL2023 AMI based on cluster version
            "amiSelectorTerms": [
                {"alias": ami_alias},
            ],
            "tags": aws_config.merged_tags(
                {"Name": f"{cluster_name}-karpenter-default-nodeclass"}
            ),
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[karpenter_release],  # Ensure Karpenter CRDs are available
        ),
    )

    default_node_labels = {**k8s_global_labels, "ol.mit.edu/gpu_node": "false"}
    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-karpenter-default-node-pool",
        api_version="karpenter.sh/v1",
        kind="NodePool",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="default",
            namespace="operations",
            labels=k8s_global_labels,
        ),
        spec={
            "template": {
                "metadata": {
                    "labels": default_node_labels,
                },
                "spec": {
                    "nodeClassRef": {
                        "group": "karpenter.k8s.aws",
                        "kind": "EC2NodeClass",
                        "name": "default",
                    },
                    "expireAfter": "720h",
                    "terminationGracePeriod": "48h",
                    "requirements": [
                        {
                            "key": "karpenter.k8s.aws/instance-category",
                            "operator": "In",
                            "values": ["m", "r", "c"],
                        },
                        {
                            "key": "karpenter.k8s.aws/instance-family",
                            "operator": "In",
                            "values": [
                                InstanceClasses.general_purpose_amd,
                                InstanceClasses.general_purpose_intel,
                                InstanceClasses.memory_optimized_amd,
                                InstanceClasses.memory_optimized_intel,
                                InstanceClasses.compute_optimized_amd,
                                InstanceClasses.compute_optimized_intel,
                            ],
                        },
                        # Keep out the smallest instance sizes
                        {
                            "key": "karpenter.k8s.aws/instance-size",
                            "operator": "NotIn",
                            "values": ["nano", "micro", "small", "medium"],
                        },
                        {
                            "key": "kubernetes.io/arch",
                            "operator": "In",
                            "values": ["amd64"],
                        },
                        {
                            "key": "kubernetes.io/os",
                            "operator": "In",
                            "values": ["linux"],
                        },
                        {
                            "key": "karpenter.sh/capacity-type",
                            "operator": "In",
                            "values": ["spot", "on-demand"],
                        },
                    ],
                },
            },
            "disruption": {
                "consolidationPolicy": "WhenEmptyOrUnderutilized",
                "consolidateAfter": "1h",
            },
            "limits": {
                "cpu": "64",
                "memory": "256Gi",
            },
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[karpenter_release],
        ),
    )

    gpu_node_labels = {**k8s_global_labels, "ol.mit.edu/gpu_node": "true"}

    # Karpenter will select this NodePool for pods with a node selector matching
    # "ol.mit.edu/gpu_node": "true". When a pod is unschedulable, Karpenter evaluates
    # all NodePools to find one that can satisfy the pod's scheduling constraints
    # (like node selectors, taints, and resource requests). Because this NodePool's
    # template adds the required label to new nodes, Karpenter will choose it over
    # the default NodePool for matching pods.
    kubernetes.apiextensions.CustomResource(
        f"{cluster_name}-karpenter-gpu-node-pool",
        api_version="karpenter.sh/v1",
        kind="NodePool",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="gpu",
            namespace="operations",
            labels=k8s_global_labels,
        ),
        spec={
            "template": {
                "metadata": {
                    "labels": gpu_node_labels,
                },
                "spec": {
                    "nodeClassRef": {
                        "group": "karpenter.k8s.aws",
                        "kind": "EC2NodeClass",
                        "name": "default",
                    },
                    "taints": [
                        {
                            "key": "ol.mit.edu/gpu_node",
                            "value": "true",
                            "effect": "NoSchedule",
                        }
                    ],
                    "expireAfter": "168h",
                    "terminationGracePeriod": "30m",
                    "requirements": [
                        {
                            "key": "node.kubernetes.io/instance-type",
                            "operator": "In",
                            "values": [
                                InstanceTypes.gpu_xlarge,
                                InstanceTypes.gpu_2xlarge,
                            ],
                        },
                        {
                            "key": "kubernetes.io/arch",
                            "operator": "In",
                            "values": ["amd64"],
                        },
                        {
                            "key": "kubernetes.io/os",
                            "operator": "In",
                            "values": ["linux"],
                        },
                        {
                            "key": "karpenter.sh/capacity-type",
                            "operator": "In",
                            "values": ["spot"],
                        },
                    ],
                },
            },
            "disruption": {
                "consolidationPolicy": "WhenEmptyOrUnderutilized",
                "consolidateAfter": "1h",
            },
            "limits": {
                "cpu": "32",
                "memory": "128Gi",
            },
        },
        opts=ResourceOptions(
            provider=k8s_provider,
            depends_on=[karpenter_release],
        ),
    )
