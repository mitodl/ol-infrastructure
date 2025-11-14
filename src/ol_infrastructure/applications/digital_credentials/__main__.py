"""Pulumi deployment for Digital Credentials Consortium (DCC) services.

This module deploys the issuer-coordinator and signing-service for issuing
verifiable credentials using the DCC microservices architecture.
"""

import json
from pathlib import Path

import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import (
    Config,
    ResourceOptions,
    StackReference,
    export,
)
from pulumi_aws import ec2, get_caller_identity

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixRoute,
    OLApisixRouteConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

setup_vault_provider()
aws_account = get_caller_identity()
stack_info = parse_stack()
digital_credentials_config = Config("digital-credentials")

# Stack references
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
vault_mount_stack = StackReference(
    f"substructure.vault.static_mounts.operations.{stack_info.name}"
)

apps_vpc = network_stack.require_output("applications_vpc")
k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]
digital_credentials_vault_kv_path = vault_mount_stack.require_output(
    "digital_credentials_kv"
)["path"]

aws_config = AWSBase(
    tags={"OU": "operations", "Environment": f"applications-{stack_info.env_suffix}"}
)

k8s_global_labels = K8sGlobalLabels(
    ou=BusinessUnit.mit_learn, service=Services.digital_credentials, stack=stack_info
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
# OLEKSAuthBinding for Vault Integration
################################################

# IAM policy for DCC services (currently no AWS resources needed)
dcc_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "s3:ListAllMyBuckets",
            "Resource": "*",
        }
    ],
}

digital_credentials_app = OLEKSAuthBinding(
    OLEKSAuthBindingConfig(
        application_name="digital-credentials",
        namespace=dcc_namespace,
        stack_info=stack_info,
        aws_config=aws_config,
        iam_policy_document=dcc_policy_document,
        vault_policy_path=Path(__file__).parent.joinpath(
            "digital_credentials_server_policy.hcl"
        ),
        cluster_identities=cluster_stack.require_output("cluster_identities"),
        vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
        irsa_service_account_name=[
            "signing-service",
            "issuer-coordinator",
        ],
        vault_sync_service_account_names="digital-credentials-vault",
        k8s_labels=K8sGlobalLabels(
            ou=BusinessUnit.operations,
            service=Services.digital_credentials,
            stack=stack_info,
        ),
    )
)

################################################
# Populate Vault with secrets from SOPS
################################################

# Read secrets from SOPS-encrypted files
signing_service_secrets = read_yaml_secrets(
    Path(f"digital_credentials/signing_service.{stack_info.env_suffix}.yaml")
)
issuer_coordinator_secrets = read_yaml_secrets(
    Path(f"digital_credentials/issuer_coordinator.{stack_info.env_suffix}.yaml")
)

# Write signing service secrets to Vault KV
vault.kv.SecretV2(
    f"signing-service-vault-secret-{stack_info.env_suffix}",
    mount=digital_credentials_vault_kv_path,
    name="signing-service",
    data_json=json.dumps(signing_service_secrets),
)

# Write issuer coordinator secrets to Vault KV
vault.kv.SecretV2(
    f"issuer-coordinator-vault-secret-{stack_info.env_suffix}",
    mount=digital_credentials_vault_kv_path,
    name="issuer-coordinator",
    data_json=json.dumps(issuer_coordinator_secrets),
)

################################################
# K8s/Vault Resources
################################################

vault_k8s_resources = digital_credentials_app.vault_k8s_resources

################################################
# Signing Service Deployment
################################################

# ConfigMap for signing keys (multi-tenant support) - synced from Vault
signing_service_secret_name = (
    "signing-service-secrets"  # pragma: allowlist secret  # noqa: S105
)
signing_service_vault_secret = OLVaultK8SSecret(
    f"signing-service-vault-secret-sync-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="signing-service-vault-secret",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=signing_service_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=digital_credentials_vault_kv_path,
        mount_type="kv-v2",
        path="signing-service",
        templates={
            key: f'{{{{ get .Secrets.tenants "{key}" }}}}'
            for key in signing_service_secrets.get("tenants", {})
        },
        refresh_after="1h",
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, depends_on=vault_k8s_resources),
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
                                secret_ref=kubernetes.core.v1.SecretEnvSourceArgs(
                                    name=signing_service_secret_name
                                )
                            )
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "100m", "memory": "128Mi"},
                            limits={"cpu": "500m", "memory": "512Mi"},
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/did-key-generator", port=4006
                            ),
                            initial_delay_seconds=10,
                            period_seconds=30,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/did-key-generator", port=4006
                            ),
                            initial_delay_seconds=5,
                            period_seconds=10,
                        ),
                    )
                ],
            ),
        ),
    ),
    opts=ResourceOptions(depends_on=[signing_service_vault_secret]),
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

# Issuer coordinator secrets synced from Vault
issuer_coordinator_secret_name = (
    "issuer-coordinator-secrets"  # pragma: allowlist secret  # noqa: S105
)
issuer_coordinator_vault_secret = OLVaultK8SSecret(
    f"issuer-coordinator-vault-secret-sync-{stack_info.env_suffix}",
    resource_config=OLVaultK8SStaticSecretConfig(
        name="issuer-coordinator-vault-secret",
        namespace=dcc_namespace,
        labels=k8s_global_labels,
        dest_secret_labels=k8s_global_labels,
        dest_secret_name=issuer_coordinator_secret_name,
        dest_secret_type="Opaque",  # pragma: allowlist secret  # noqa: S106
        mount=digital_credentials_vault_kv_path,
        mount_type="kv-v2",
        path="issuer-coordinator",
        templates={
            **{
                key: f'{{{{ get .Secrets.tenant_tokens "{key}" }}}}'
                for key in issuer_coordinator_secrets.get("tenant_tokens", {})
            },
        },
        refresh_after="1h",
        vaultauth=vault_k8s_resources.auth_name,
    ),
    opts=ResourceOptions(delete_before_replace=True, depends_on=vault_k8s_resources),
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
                                    name=issuer_coordinator_secret_name
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
    opts=ResourceOptions(
        depends_on=[signing_service_service, issuer_coordinator_vault_secret]
    ),
)

issuer_coordinator_service_name = "issuer-coordinator"
issuer_coordinator_service = kubernetes.core.v1.Service(
    f"issuer-coordinator-svc-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=issuer_coordinator_service_name,
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

# Create APISix route with key-auth authentication
issuer_coordinator_apisix_route = OLApisixRoute(
    f"issuer-coordinator-{stack_info.env_suffix}-apisix-route",
    k8s_namespace=dcc_namespace,
    k8s_labels=k8s_global_labels,
    route_configs=[
        OLApisixRouteConfig(
            route_name="issuer-coordinator-protected",
            priority=10,
            hosts=[issuer_coordinator_domain],
            paths=["/*"],
            backend_service_name=issuer_coordinator_service_name,
            backend_service_port="http",
            backend_resolve_granularity="service",
        ),
    ],
    opts=ResourceOptions(depends_on=[issuer_coordinator_service]),
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
