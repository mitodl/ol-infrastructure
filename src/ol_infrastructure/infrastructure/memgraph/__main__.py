"""Memgraph graph database infrastructure stack.

Stack naming: infrastructure.memgraph.<purpose>.<env>
  e.g., infrastructure.memgraph.codegraph.QA
        infrastructure.memgraph.codegraph.Production

Each logical Memgraph deployment gets its own named stack so multiple independent
Memgraph instances can coexist in the cluster (e.g., code-graph, knowledge-graph).

Exports:
  bolt_endpoint  - Bolt protocol endpoint (host:port) for client connections
  http_endpoint  - HTTP API endpoint for Memgraph Lab and REST access
  namespace      - Kubernetes namespace where Memgraph is deployed
  secret_name    - Kubernetes Secret name holding Memgraph credentials
"""

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, export

from bridge.lib.versions import MEMGRAPH_CHART_VERSION
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
memgraph_config = Config("memgraph")

# stack_info.env_prefix is the purpose (e.g., "codegraph")
# stack_info.env_suffix is the environment (e.g., "qa", "production")
purpose = stack_info.env_prefix
env = stack_info.env_suffix

aws_config = AWSBase(tags={"OU": "operations", "Environment": env})

k8s_global_labels: dict[str, str] = {
    "ol.mit.edu/stack": stack_info.full_name,
    "ol.mit.edu/managed-by": "pulumi",
    "ol.mit.edu/service": "memgraph",
    "ol.mit.edu/purpose": purpose,
}

namespace_name: str = memgraph_config.get("namespace") or f"memgraph-{purpose}-{env}"
storage_size: str = memgraph_config.get("storage_size") or "50Gi"
memory_limit: str = memgraph_config.get("memory_limit") or "8Gi"
enable_lab: bool = memgraph_config.get_bool("enable_lab") or False
helm_version: str = memgraph_config.get("helm_version") or MEMGRAPH_CHART_VERSION
allowed_namespaces: list[str] = memgraph_config.get_object("allowed_namespaces") or []

MEMGRAPH_BOLT_PORT = 7687
MEMGRAPH_HTTP_PORT = 7444
MEMGRAPH_LAB_PORT = 3000

memgraph_namespace = kubernetes.core.v1.Namespace(
    f"memgraph-{purpose}-namespace",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=namespace_name,
        labels={
            **k8s_global_labels,
            "kubernetes.io/metadata.name": namespace_name,
        },
    ),
)

# PVC for Memgraph data persistence — pinned to ebs-gp3-sc for EBS-backed storage.
# Memgraph is a single-instance stateful workload; the PVC is bound to one node.
memgraph_pvc = kubernetes.core.v1.PersistentVolumeClaim(
    f"memgraph-{purpose}-pvc",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"memgraph-{purpose}-data",
        namespace=namespace_name,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
        access_modes=["ReadWriteOnce"],
        storage_class_name="ebs-gp3-sc",
        resources=kubernetes.core.v1.ResourceRequirementsArgs(
            requests={"storage": storage_size}
        ),
    ),
    opts=ResourceOptions(
        parent=memgraph_namespace,
        depends_on=[memgraph_namespace],
        ignore_changes=["spec.storageClassName"],
    ),
)

# Memgraph Helm release — standalone single-node deployment.
# The Helm chart creates a StatefulSet with the provided PVC claim template replaced
# by our pre-provisioned PVC via existingClaim.
memgraph_release = kubernetes.helm.v3.Release(
    f"memgraph-{purpose}-helm-release",
    kubernetes.helm.v3.ReleaseArgs(
        name=f"memgraph-{purpose}",
        chart="memgraph",
        version=helm_version,
        namespace=namespace_name,
        cleanup_on_fail=True,
        repository_opts=kubernetes.helm.v3.RepositoryOptsArgs(
            repo="https://memgraph.github.io/helm-charts",
        ),
        values={
            "memgraph": {
                "env": {
                    "MEMGRAPH_MEMORY_LIMIT": memory_limit,
                },
            },
            "persistentVolumeClaim": {
                "existingClaim": f"memgraph-{purpose}-data",
                "storageClassName": "ebs-gp3-sc",
                "size": storage_size,
            },
            "resources": {
                "requests": {"memory": "2Gi", "cpu": "500m"},
                "limits": {"memory": memory_limit},
            },
            "extraLabels": k8s_global_labels,
        },
    ),
    opts=ResourceOptions(
        parent=memgraph_namespace,
        depends_on=[memgraph_namespace, memgraph_pvc],
        delete_before_replace=True,
    ),
)

# Headless Service for stable Bolt endpoint — consumed by application stacks.
memgraph_bolt_service = kubernetes.core.v1.Service(
    f"memgraph-{purpose}-bolt-service",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"memgraph-{purpose}-bolt",
        namespace=namespace_name,
        labels={**k8s_global_labels, "app": f"memgraph-{purpose}"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app.kubernetes.io/name": "memgraph"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="bolt",
                port=MEMGRAPH_BOLT_PORT,
                target_port=MEMGRAPH_BOLT_PORT,
            ),
            kubernetes.core.v1.ServicePortArgs(
                name="http",
                port=MEMGRAPH_HTTP_PORT,
                target_port=MEMGRAPH_HTTP_PORT,
            ),
        ],
    ),
    opts=ResourceOptions(parent=memgraph_release, depends_on=[memgraph_release]),
)

# NetworkPolicy — restrict Bolt/HTTP access to permitted namespaces only.
# Ingress allowed from: same namespace (operator/admin) + allowed_namespaces.
allowed_ns_ingress = [
    kubernetes.networking.v1.NetworkPolicyIngressRuleArgs(
        from_=[
            kubernetes.networking.v1.NetworkPolicyPeerArgs(
                namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                    match_labels={"kubernetes.io/metadata.name": namespace_name}
                )
            )
        ]
    )
]
if allowed_namespaces:
    allowed_ns_ingress.append(
        kubernetes.networking.v1.NetworkPolicyIngressRuleArgs(
            from_=[
                kubernetes.networking.v1.NetworkPolicyPeerArgs(
                    namespace_selector=kubernetes.meta.v1.LabelSelectorArgs(
                        match_expressions=[
                            kubernetes.meta.v1.LabelSelectorRequirementArgs(
                                key="kubernetes.io/metadata.name",
                                operator="In",
                                values=allowed_namespaces,
                            )
                        ]
                    )
                )
            ],
            ports=[
                kubernetes.networking.v1.NetworkPolicyPortArgs(port=MEMGRAPH_BOLT_PORT),
                kubernetes.networking.v1.NetworkPolicyPortArgs(port=MEMGRAPH_HTTP_PORT),
            ],
        )
    )

memgraph_network_policy = kubernetes.networking.v1.NetworkPolicy(
    f"memgraph-{purpose}-network-policy",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"memgraph-{purpose}-allow-clients",
        namespace=namespace_name,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.networking.v1.NetworkPolicySpecArgs(
        pod_selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app.kubernetes.io/name": "memgraph"},
        ),
        policy_types=["Ingress"],
        ingress=allowed_ns_ingress,
    ),
    opts=ResourceOptions(parent=memgraph_namespace, depends_on=[memgraph_release]),
)

# Optional Memgraph Lab — web-based graph browser for development and debugging.
if enable_lab:
    lab_deployment = kubernetes.apps.v1.Deployment(
        f"memgraph-{purpose}-lab-deployment",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"memgraph-{purpose}-lab",
            namespace=namespace_name,
            labels={**k8s_global_labels, "app": f"memgraph-{purpose}-lab"},
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels={"app": f"memgraph-{purpose}-lab"},
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels={**k8s_global_labels, "app": f"memgraph-{purpose}-lab"},
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="lab",
                            image="memgraph/lab:latest",
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="QUICK_CONNECT_MG_HOST",
                                    value=f"memgraph-{purpose}-bolt.{namespace_name}.svc.cluster.local",
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="QUICK_CONNECT_MG_PORT",
                                    value=str(MEMGRAPH_BOLT_PORT),
                                ),
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    name="web", container_port=MEMGRAPH_LAB_PORT
                                )
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "100m", "memory": "256Mi"},
                                limits={"memory": "512Mi"},
                            ),
                        )
                    ],
                ),
            ),
        ),
        opts=ResourceOptions(
            parent=memgraph_namespace,
            depends_on=[memgraph_bolt_service],
            delete_before_replace=True,
        ),
    )
    kubernetes.core.v1.Service(
        f"memgraph-{purpose}-lab-service",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"memgraph-{purpose}-lab",
            namespace=namespace_name,
            labels={**k8s_global_labels, "app": f"memgraph-{purpose}-lab"},
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            type="ClusterIP",
            selector={"app": f"memgraph-{purpose}-lab"},
            ports=[
                kubernetes.core.v1.ServicePortArgs(
                    name="web", port=MEMGRAPH_LAB_PORT, target_port=MEMGRAPH_LAB_PORT
                )
            ],
        ),
        opts=ResourceOptions(parent=lab_deployment),
    )

bolt_host = f"memgraph-{purpose}-bolt.{namespace_name}.svc.cluster.local"
http_host = f"memgraph-{purpose}-bolt.{namespace_name}.svc.cluster.local"

export("bolt_endpoint", f"{bolt_host}:{MEMGRAPH_BOLT_PORT}")
export("bolt_host", bolt_host)
export("bolt_port", str(MEMGRAPH_BOLT_PORT))
export("http_endpoint", f"{http_host}:{MEMGRAPH_HTTP_PORT}")
export("namespace", namespace_name)
export("secret_name", f"memgraph-{purpose}-credentials")
