# ruff: noqa: E501, PLR0913, FIX002, TD002
import pulumi_aws as aws
import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export
from pulumi_eks import Cluster

from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase


def setup_external_dns(
    cluster_name: str,
    cluster: Cluster,
    aws_account,
    aws_config: AWSBase,
    k8s_provider: kubernetes.Provider,
    operations_namespace: kubernetes.core.v1.Namespace,
    node_groups: list[eks.NodeGroupV2],
    k8s_global_labels: dict[str, str],
    operations_tolerations: list[dict[str, str]],
    versions: dict[str, str],
    eks_config: Config,
):
    """
    Configure and install external-dns.

    :param cluster_name: The name of the EKS cluster.
    :param cluster: The EKS cluster object.
    :param aws_account: The AWS account object.
    :param aws_config: The AWS configuration object.
    :param k8s_provider: The Kubernetes provider for Pulumi.
    :param operations_namespace: The operations namespace object.
    :param node_groups: A list of EKS node groups.
    :param k8s_global_labels: A dictionary of global labels to apply to Kubernetes resources.
    :param operations_tolerations: A list of tolerations for scheduling on operations nodes.
    :param versions: A dictionary of component versions.
    :param eks_config: The EKS configuration object.
    """
    # Ref: https://github.com/kubernetes-sigs/external-dns
    # Ref: https://github.com/kubernetes-sigs/external-dns/blob/master/docs/tutorials/aws.md
    # Ref: https://github.com/kubernetes-sigs/external-dns/blob/master/docs/sources/traefik-proxy.md
    external_dns_parliament_config = {
        "UNKNOWN_FEDERATION_SOURCE": {"ignore_locations": [{"principal": "federated"}]},
        "PERMISSIONS_MANAGEMENT_ACTIONS": {"ignore_locations": []},
        "MALFORMED": {"ignore_lcoations": []},
        "RESOURCE_STAR": {"ignore_locations": []},
    }
    external_dns_role_config = OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=cluster_name,
        cluster_identities=cluster.eks_cluster.identities,
        description="Trust role for allowing external-dns to modify route53 "
        "resources from within the cluster.",
        policy_operator="StringEquals",
        role_name="external-dns",
        service_account_identifier="system:serviceaccount:operations:external-dns",
        tags=aws_config.tags,
    )
    external_dns_role = OLEKSTrustRole(
        f"{cluster_name}-external-dns-trust-role",
        role_config=external_dns_role_config,
        opts=ResourceOptions(parent=cluster, depends_on=cluster),
    )
    external_dns_policy_document = {
        "Version": IAM_POLICY_VERSION,
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["route53:ChangeResourceRecordSets"],
                "Resource": [
                    # TODO: @Ardiea interpolate with explicit zone IDs
                    # More difficult than it sounds
                    "arn:aws:route53:::hostedzone/*"
                ],
            },
            {
                "Effect": "Allow",
                "Action": [
                    "route53:ListHostedZones",
                    "route53:ListResourceRecordSets",
                    "route53:ListTagsForResource",
                ],
                "Resource": ["*"],
            },
        ],
    }
    export("allowed_dns_zones", eks_config.require_object("allowed_dns_zones"))

    external_dns_policy = aws.iam.Policy(
        f"{cluster_name}-external-dns-policy",
        name=f"{cluster_name}-external-dns-policy",
        path=f"/ol-infrastructure/eks/{cluster_name}/",
        policy=lint_iam_policy(
            external_dns_policy_document,
            parliament_config=external_dns_parliament_config,
            stringify=True,
        ),
        opts=ResourceOptions(parent=external_dns_role, depends_on=cluster),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-external-dns-attachment",
        policy_arn=external_dns_policy.arn,
        role=external_dns_role.role.id,
        opts=ResourceOptions(parent=external_dns_role),
    )
    kubernetes.helm.v3.Release(
        f"{cluster_name}-external-dns-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="external-dns",
            chart="external-dns",
            version=versions["EXTERNAL_DNS_CHART"],
            namespace="operations",
            cleanup_on_fail=True,
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://kubernetes-sigs.github.io/external-dns/",
            ),
            values={
                "image": {
                    "pullPolicy": "Always",
                },
                "commonLabels": k8s_global_labels,
                "podLabels": k8s_global_labels,
                "tolerations": operations_tolerations,
                "serviceAccount": {
                    "create": True,
                    "name": "external-dns",
                    "annotations": {
                        # Allows external-dns to make aws API calls to route53
                        "eks.amazonaws.com/role-arn": external_dns_role.role.arn.apply(
                            lambda arn: f"{arn}"
                        ),
                    },
                },
                "logLevel": "info",
                "policy": "sync",
                # Configure external-dns to only look at gateway resources
                # disables support for monitoring services or legacy ingress resources
                "sources": [
                    "service",
                    "gateway-udproute",
                    "gateway-tcproute",
                    "gateway-grpcroute",
                    "gateway-httproute",
                    "gateway-tlsroute",
                ],
                # Create a txt record to indicate provenance of the record(s)
                "txtOwnerId": cluster_name,
                # Need to explicitly turn off support for legacy traefik ingress services
                # to avoid an annoying bug
                "extraArgs": [
                    "--traefik-disable-legacy",
                ],
                # Limit the dns zones that external dns knows about
                "domainFilters": eks_config.require_object("allowed_dns_zones"),
                "resources": {
                    "requests": {
                        "memory": "128Mi",
                        "cpu": "10m",
                    },
                    "limits": {
                        "memory": "128Mi",
                    },
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            delete_before_replace=True,
            depends_on=[cluster, node_groups[0], operations_namespace],
        ),
    )
