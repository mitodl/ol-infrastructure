# ruff: noqa: E501, PLR0913, FIX002, TD002
import pulumi_aws as aws
import pulumi_eks as eks
import pulumi_kubernetes as kubernetes
from pulumi import ResourceOptions, export
from pulumi_eks import Cluster

from ol_infrastructure.components.aws.eks import OLEKSTrustRole, OLEKSTrustRoleConfig
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.ol_types import AWSBase


def setup_cert_manager(
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
):
    """
    Configure and install cert-manager.

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
    """
    cert_manager_parliament_config = {
        "UNKNOWN_FEDERATION_SOURCE": {"ignore_locations": [{"principal": "federated"}]},
        "PERMISSIONS_MANAGEMENT_ACTIONS": {"ignore_locations": []},
        "MALFORMED": {"ignore_lcoations": []},
        "RESOURCE_STAR": {"ignore_lcoations": []},
    }
    # Cert manager uses DNS txt records to confirm that we control the
    # domains that we are requesting certificates for.
    # Ref: https://cert-manager.io/docs/configuration/acme/dns01/route53/#set-up-an-iam-role
    cert_manager_role_config = OLEKSTrustRoleConfig(
        account_id=aws_account.account_id,
        cluster_name=cluster_name,
        cluster_identities=cluster.eks_cluster.identities,
        description="Trust role for allowing cert-manager to modify route53 "
        "resources from within the cluster.",
        policy_operator="StringEquals",
        role_name="cert-manager",
        service_account_identifier="system:serviceaccount:operations:cert-manager",
        tags=aws_config.tags,
    )
    cert_manager_role = OLEKSTrustRole(
        f"{cluster_name}-cert-manager-trust-role",
        role_config=cert_manager_role_config,
        opts=ResourceOptions(parent=cluster, depends_on=cluster),
    )
    export("cert_manager_arn", cert_manager_role.role.arn)

    cert_manager_policy_document = {
        "Version": IAM_POLICY_VERSION,
        "Statement": [
            {
                "Effect": "Allow",
                "Action": "route53:GetChange",
                "Resource": "arn:aws:route53:::change/*",
            },
            {
                "Effect": "Allow",
                "Action": [
                    "route53:ChangeResourceRecordSets",
                    "route53:ListResourceRecordSets",
                ],
                # TODO: @Ardiea interpolate with explicit zone IDs
                # More difficult than it sounds
                "Resource": "arn:aws:route53:::hostedzone/*",
            },
            {
                "Effect": "Allow",
                "Action": "route53:ListHostedZonesByName",
                "Resource": "*",
            },
        ],
    }

    cert_manager_policy = aws.iam.Policy(
        f"{cluster_name}-cert-manager-policy",
        name=f"{cluster_name}-cert-manager-policy",
        path=f"/ol-infrastructure/eks/{cluster_name}/",
        policy=lint_iam_policy(
            cert_manager_policy_document,
            parliament_config=cert_manager_parliament_config,
            stringify=True,
        ),
        opts=ResourceOptions(parent=cert_manager_role, depends_on=cluster),
    )
    aws.iam.RolePolicyAttachment(
        f"{cluster_name}-cert-manager-attachment",
        policy_arn=cert_manager_policy.arn,
        role=cert_manager_role.role.id,
        opts=ResourceOptions(parent=cert_manager_role),
    )

    default_cert_manager_resources = {
        "requests": {
            "memory": "64Mi",
            "cpu": "10m",
        },
        "limits": {
            "memory": "128Mi",
            "cpu": "50m",
        },
    }

    # Ref: https://cert-manager.io/docs/installation/
    kubernetes.helm.v3.Release(
        f"{cluster_name}-cert-manager-helm-release",
        kubernetes.helm.v3.ReleaseArgs(
            name="cert-manager",
            chart="cert-manager",
            version=versions["CERT_MANAGER_CHART"],
            namespace="operations",
            repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
                repo="https://charts.jetstack.io",
            ),
            cleanup_on_fail=True,
            skip_await=False,
            values={
                "crds": {
                    "enabled": True,
                    "keep": True,
                },
                "global": {
                    "commonLabels": k8s_global_labels,
                },
                "resources": default_cert_manager_resources,
                "tolerations": operations_tolerations,
                "replicaCount": 1,
                "enableCertificateOwnerRef": True,
                "prometheus": {
                    "enabled": False,
                },
                "config": {
                    "apiVersion": "controller.config.cert-manager.io/v1alpha1",
                    "kind": "ControllerConfiguration",
                    "enableGatewayAPI": True,
                },
                "webhook": {
                    "resources": default_cert_manager_resources,
                    "tolerations": operations_tolerations,
                },
                "cainjector": {
                    "resources": default_cert_manager_resources,
                    "tolerations": operations_tolerations,
                },
                "serviceAccount": {
                    "create": True,
                    "name": "cert-manager",
                    "annotations": {
                        # Allows cert-manager to make aws API calls to route53
                        "eks.amazonaws.com/role-arn": cert_manager_role.role.arn.apply(
                            lambda arn: f"{arn}"
                        ),
                    },
                },
            },
        ),
        opts=ResourceOptions(
            provider=k8s_provider,
            parent=operations_namespace,
            depends_on=[cluster, node_groups[0]],
            delete_before_replace=True,
        ),
    )
