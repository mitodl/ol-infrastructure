"""
Pulumi stack to build and deploy kubewatch webhook handler.

This stack:
1. Builds a Docker image for the webhook handler
2. Pushes it to ECR
3. Deploys the webhook handler to Kubernetes
4. Creates necessary secrets and services
"""

from pathlib import Path

import pulumi_aws as aws
import pulumi_docker as docker
import pulumi_kubernetes as kubernetes
from pulumi import Config, Output, StackReference, export, log

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit, Environment
from ol_infrastructure.lib.pulumi_helper import parse_stack

# Get stack info
stack_info = parse_stack()
log.info(f"{stack_info=}")

# AWS and Kubernetes configuration
aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": Environment.operations,
    },
)

# Reference the EKS cluster stack
cluster_stack = StackReference(
    f"infrastructure.aws.eks.{stack_info.env_prefix}.{stack_info.name}"
)

# Setup Kubernetes provider
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

# Configuration
webhook_config = Config("kubewatch_webhook")
vault_config = Config("vault")

# Namespace filtering (kubewatch watches all, webhook handler filters)
watched_namespaces = webhook_config.get("watched_namespaces") or ""

# Get filtering patterns from config
ignored_label_patterns = webhook_config.get("ignored_label_patterns") or "celery"

# Read secrets
webhook_secrets = read_yaml_secrets(
    Path(f"kubewatch/secrets.{stack_info.env_prefix}.{stack_info.env_suffix}.yaml"),
)

# ECR repository for webhook handler image (per-environment)
ecr_repository_name = f"kubewatch-webhook-handler-{stack_info.env_suffix.lower()}"
ecr_repository = aws.ecr.Repository(
    f"kubewatch-webhook-handler-ecr-repository-{stack_info.env_suffix}",
    name=ecr_repository_name,
    image_tag_mutability="MUTABLE",
    image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
        scan_on_push=True,
    ),
    force_delete=True,  # Allow deletion even when images are present
    tags=aws_config.tags,
)

# ECR lifecycle policy to keep only recent images
ecr_lifecycle_policy = aws.ecr.LifecyclePolicy(
    "kubewatch-webhook-handler-ecr-lifecycle",
    repository=ecr_repository.name,
    policy="""{
        "rules": [{
            "rulePriority": 1,
            "description": "Keep last 10 images",
            "selection": {
                "tagStatus": "any",
                "countType": "imageCountMoreThan",
                "countNumber": 10
            },
            "action": {
                "type": "expire"
            }
        }]
    }""",
)

# Get ECR authorization token
ecr_auth_token = aws.ecr.get_authorization_token_output(
    registry_id=ecr_repository.registry_id
)

# Build and push Docker image
webhook_image = docker.Image(
    "kubewatch-webhook-handler-image",
    build=docker.DockerBuildArgs(
        context=str(Path(__file__).parent),
        dockerfile=str(Path(__file__).parent / "Dockerfile"),
        platform="linux/amd64",
    ),
    image_name=ecr_repository.repository_url.apply(lambda url: f"{url}:latest"),
    registry=docker.RegistryArgs(
        server=ecr_repository.repository_url,
        username=ecr_auth_token.user_name,
        password=ecr_auth_token.password,
    ),
)

# Kubernetes namespace
kubewatch_namespace = "kubewatch"

# Per-environment resource names
webhook_resource_name = f"kubewatch-webhook-{stack_info.env_suffix.lower()}"
webhook_secret_name = f"kubewatch-webhook-secret-{stack_info.env_suffix.lower()}"
webhook_service_account_name = f"kubewatch-webhook-{stack_info.env_suffix.lower()}"

# Create ServiceAccount for webhook handler
webhook_service_account = kubernetes.core.v1.ServiceAccount(
    f"kubewatch-webhook-serviceaccount-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=webhook_service_account_name,
        namespace=kubewatch_namespace,
    ),
)

# Create ClusterRole with permissions to read deployments across all namespaces
webhook_cluster_role = kubernetes.rbac.v1.ClusterRole(
    f"kubewatch-webhook-clusterrole-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"kubewatch-webhook-{stack_info.env_suffix.lower()}",
    ),
    rules=[
        kubernetes.rbac.v1.PolicyRuleArgs(
            api_groups=["apps"],
            resources=["deployments"],
            verbs=["get", "list", "watch"],
        ),
    ],
)

# Bind the ClusterRole to the ServiceAccount
webhook_cluster_role_binding = kubernetes.rbac.v1.ClusterRoleBinding(
    f"kubewatch-webhook-clusterrolebinding-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"kubewatch-webhook-{stack_info.env_suffix.lower()}",
    ),
    role_ref=kubernetes.rbac.v1.RoleRefArgs(
        api_group="rbac.authorization.k8s.io",
        kind="ClusterRole",
        name=f"kubewatch-webhook-{stack_info.env_suffix.lower()}",
    ),
    subjects=[
        kubernetes.rbac.v1.SubjectArgs(
            kind="ServiceAccount",
            name=webhook_service_account_name,
            namespace=kubewatch_namespace,
        ),
    ],
)

# Create Kubernetes secret for Slack token
webhook_secret = kubernetes.core.v1.Secret(
    f"kubewatch-webhook-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=webhook_secret_name,
        namespace=kubewatch_namespace,
    ),
    string_data={
        "slack-token": webhook_secrets["slack-token"],
    },
)

# Create Kubernetes deployment for webhook handler
webhook_deployment = kubernetes.apps.v1.Deployment(
    f"kubewatch-webhook-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=webhook_resource_name,
        namespace=kubewatch_namespace,
        labels={
            "app": webhook_resource_name,
            "ou": BusinessUnit.operations,
            "environment": stack_info.env_suffix,
        },
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        replicas=2,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": webhook_resource_name},
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={"app": webhook_resource_name},
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                service_account_name=webhook_service_account_name,
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="webhook-handler",
                        image=webhook_image.image_name,
                        ports=[
                            kubernetes.core.v1.ContainerPortArgs(
                                container_port=8080,
                                name="http",
                            ),
                        ],
                        env=[
                            kubernetes.core.v1.EnvVarArgs(
                                name="SLACK_TOKEN",
                                value_from=kubernetes.core.v1.EnvVarSourceArgs(
                                    secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                                        name=webhook_secret_name,
                                        key="slack-token",
                                    ),
                                ),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="SLACK_CHANNEL",
                                value=Config("slack").require("channel_name"),
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="WATCHED_NAMESPACES",
                                value=watched_namespaces,
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="IGNORED_LABEL_PATTERNS",
                                value=ignored_label_patterns,
                            ),
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "100m",
                                "memory": "128Mi",
                            },
                            limits={
                                "cpu": "500m",
                                "memory": "512Mi",
                            },
                        ),
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/health",
                                port=8080,
                            ),
                            initial_delay_seconds=10,
                            period_seconds=30,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/health",
                                port=8080,
                            ),
                            initial_delay_seconds=5,
                            period_seconds=10,
                        ),
                    ),
                ],
            ),
        ),
    ),
)

# Create Kubernetes service for webhook handler
webhook_service = kubernetes.core.v1.Service(
    f"kubewatch-webhook-service-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=webhook_resource_name,
        namespace=kubewatch_namespace,
        labels={
            "app": webhook_resource_name,
        },
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        selector={"app": webhook_resource_name},
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                port=80,
                target_port=8080,
                protocol="TCP",
                name="http",
            ),
        ],
        type="ClusterIP",
    ),
)

# Export outputs
export("ecr_repository_url", ecr_repository.repository_url)
export("webhook_image", webhook_image.image_name)
export(
    "webhook_service_url",
    Output.concat(
        "http://",
        webhook_resource_name,
        ".",
        kubewatch_namespace,
        ".svc.cluster.local/webhook/kubewatch",
    ),
)
