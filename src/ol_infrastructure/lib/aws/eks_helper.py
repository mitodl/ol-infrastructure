from pulumi import StackReference


def check_cluster_namespace(cluster_stack: StackReference, namespace: str):
    """Verify that a namespace is available in an EKS cluster.

    :param cluster_stack: object representing the infrastructure.aws.eks
        stack that is being checked.
    :type cluster_stakc: pulumi.StackReference

    :param namespace: The name of the namespace to verify.
    :type namespace: str
    """
    if namespace not in cluster_stack.require_output("namespaces"):
        msg = f"Namespace: {namespace} is not present in the EKS cluster stack."
        raise ValueError(msg)
