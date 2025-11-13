"""Pulumi deployment for Digital Credentials Consortium (DCC) services.

This module deploys the issuer-coordinator and signing-service for issuing
verifiable credentials using the DCC microservices architecture.
"""

from pathlib import Path

import pulumi_kubernetes as kubernetes
from pulumi import (
    Config,
    Output,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import ec2, get_caller_identity

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.services.k8s import (
    OLApisixPluginConfig,
    OLApisixRoute,
    OLApisixRouteConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack

aws_account = get_caller_identity()
stack_info = parse_stack()
digital_credentials_config = Config("digital-credentials")

# Stack references
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")

apps_vpc = network_stack.require_output("applications_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"applications-{stack_info.env_suffix}"}
)

k8s_global_labels = K8sGlobalLabels(
    ou=BusinessUnit.operations, service=Services.digital_credentials, stack=stack_info
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Namespace setup
dcc_namespace = "digital-credentials"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(dcc_namespace, ns)
)

################################################
# Security Groups
################################################

dcc_security_group = ec2.SecurityGroup(
    f"dcc-security-group-{stack_info.env_suffix}",
    name=f"dcc-security-group-{stack_info.env_suffix}",
    description="Access control for DCC services (issuer-coordinator, signing-service)",
    egress=default_psg_egress_args,
    ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
    vpc_id=apps_vpc["id"],
    tags=aws_config.tags,
)

################################################
# Signing Service Deployment
################################################

signing_service_secrets = read_yaml_secrets(
    Path(f"digital_credentials/signing_service.{stack_info.env_suffix}.yaml")
)

# ConfigMap for signing keys (multi-tenant support)
signing_service_configmap = kubernetes.core.v1.ConfigMap(
    f"signing-service-config-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="signing-service-config",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
    ),
    data=signing_service_secrets.get("tenants", {}),
)

signing_service_deployment = kubernetes.apps.v1.Deployment(
    f"signing-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="signing-service",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=digital_credentials_config.get_int("signing_service_replicas") or 2,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": "signing-service", **k8s_global_labels}
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={"app": "signing-service", **k8s_global_labels}
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="signing-service",
                        image=digital_credentials_config.get("signing_service_image")
                        or "digitalcredentials/signing-service:1.0.0",
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=4006, name="http"
                            )
                        ],
                        env_from=[
                            kubernetes.core.v1.EnvFromSourceArgs(
                                config_map_ref=kubernetes.core.v1.ConfigMapEnvSourceArgs(
                                    name=signing_service_configmap.metadata.name
                                )
                            )
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "100m", "memory": "128Mi"},
                            limits={"cpu": "500m", "memory": "512Mi"},
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/", port=4006
                            ),
                            initial_delay_seconds=10,
                            period_seconds=30,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/", port=4006
                            ),
                            initial_delay_seconds=5,
                            period_seconds=10,
                        ),
                    )
                ],
            ),
        ),
    ),
)

signing_service_service = kubernetes.core.v1.Service(
    f"signing-service-svc-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="signing-service",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": "signing-service"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="http", port=4006, target_port=4006, protocol="TCP"
            )
        ],
    ),
)

################################################
# Issuer Coordinator Deployment
################################################

issuer_coordinator_secrets = read_yaml_secrets(
    Path(f"digital_credentials/issuer_coordinator.{stack_info.env_suffix}.yaml")
)

# Secret for tenant tokens
issuer_coordinator_secret = kubernetes.core.v1.Secret(
    f"issuer-coordinator-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="issuer-coordinator-secret",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
    ),
    string_data=issuer_coordinator_secrets.get("tenant_tokens", {}),
)

# APISix Consumer for ingress authentication
issuer_coordinator_apisix_consumer = kubernetes.apiextensions.CustomResource(
    f"issuer-coordinator-{stack_info.env_suffix}-apisix-consumer",
    api_version="apisix.apache.org/v2",
    kind="ApisixConsumer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="issuer-coordinator-client",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "authParameter": {
            "keyAuth": {
                "value": {
                    "key": Output.secret(
                        issuer_coordinator_secrets.get("apisix_token", "")
                    ),
                },
            },
        },
    },
)

issuer_coordinator_deployment = kubernetes.apps.v1.Deployment(
    f"issuer-coordinator-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="issuer-coordinator",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=digital_credentials_config.get_int("issuer_coordinator_replicas") or 2,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": "issuer-coordinator", **k8s_global_labels}
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={"app": "issuer-coordinator", **k8s_global_labels}
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="issuer-coordinator",
                        image=digital_credentials_config.get("issuer_coordinator_image")
                        or "digitalcredentials/issuer-coordinator:1.0.0",
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=4005, name="http"
                            )
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="SIGNING_SERVICE",
                                value=f"signing-service.{dcc_namespace}.svc.cluster.local:4006",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="ENABLE_STATUS_SERVICE",
                                value=digital_credentials_config.get(
                                    "enable_status_service"
                                )
                                or "false",
                            ),
                        ],
                        env_from=[
                            kubernetes.core.v1.EnvFromSourceArgs(
                                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                    name=issuer_coordinator_secret.metadata.name
                                )
                            )
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "100m", "memory": "128Mi"},
                            limits={"cpu": "500m", "memory": "512Mi"},
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/", port=4005
                            ),
                            initial_delay_seconds=10,
                            period_seconds=30,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/", port=4005
                            ),
                            initial_delay_seconds=5,
                            period_seconds=10,
                        ),
                    )
                ],
            ),
        ),
    ),
    opts=ResourceOptions(depends_on=[signing_service_service]),
)

issuer_coordinator_service = kubernetes.core.v1.Service(
    f"issuer-coordinator-svc-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="issuer-coordinator",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        type="ClusterIP",
        selector={"app": "issuer-coordinator"},
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                name="http", port=4005, target_port=4005, protocol="TCP"
            )
        ],
    ),
)

################################################
# APISix Ingress Route
################################################

# Get domain configuration
issuer_coordinator_domain = (
    digital_credentials_config.get("issuer_coordinator_domain")
    or f"issuer-coordinator-{stack_info.env_suffix}.odl.mit.edu"
)
apisix_ingress_class = (
    digital_credentials_config.get("apisix_ingress_class") or "apisix"
)

# Create APISix route with key-auth authentication
issuer_coordinator_apisix_route = OLApisixRoute(
    f"issuer-coordinator-{stack_info.env_suffix}-apisix-route",
    k8s_namespace=dcc_namespace,
    k8s_labels=k8s_global_labels,
    ingress_class_name=apisix_ingress_class,
    route_configs=[
        OLApisixRouteConfig(
            route_name="issuer-coordinator-protected",
            priority=10,
            plugins=[
                OLApisixPluginConfig(
                    name="key-auth",
                    config={
                        "header": "X-API-Key",
                    },
                ),
            ],
            hosts=[issuer_coordinator_domain],
            paths=["/*"],
            backend_service_name=issuer_coordinator_service.metadata.name,
            backend_service_port="http",
            backend_resolve_granularity="service",
        ),
    ],
    opts=ResourceOptions(
        depends_on=[issuer_coordinator_service, issuer_coordinator_apisix_consumer]
    ),
)

################################################
# Exports
################################################

export(
    "digital_credentials",
    {
        "issuer_coordinator_service": issuer_coordinator_service.metadata.name,
        "issuer_coordinator_domain": issuer_coordinator_domain,
        "signing_service_service": signing_service_service.metadata.name,
        "namespace": dcc_namespace,
    },
)
