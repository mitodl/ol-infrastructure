"""
Pulumi stack to deploy the release bot.

This stack:
1. Creates ECR repository for the Docker image
2. Deploys the release bot to Kubernetes (image built separately)
3. Creates necessary secrets

The bot uses Slack Socket Mode (outbound WebSocket) — no Service or Ingress required.
Single replica: Socket Mode connections are not multiplexed.
"""

import json
from pathlib import Path

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pulumi import Config, StackReference, export, log

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit, Environment
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
log.info(f"{stack_info=}")

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": Environment.operations,
    },
)

cluster_stack = StackReference(
    f"infrastructure.aws.eks.{stack_info.env_prefix}.{stack_info.name}"
)
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

bot_secrets = read_yaml_secrets(
    Path(f"release_bot/secrets.{stack_info.env_prefix}.{stack_info.env_suffix}.yaml")
)
concourse_ops_secrets = read_yaml_secrets(
    Path(f"concourse/operations.{stack_info.env_suffix.lower()}.yaml")
)
github_token = concourse_ops_secrets["infrastructure/github"][
    "issues_resource_access_token"
]

bot_config = Config("release_bot")
concourse_url = bot_config.get("concourse_url") or "https://cicd.odl.mit.edu"

default_repos_config = {
    "learn-ai": {
        "pipeline": "learn-ai-pipeline",
        "repo": "mitodl/learn-ai",
        "branch": "main",
    },
    "micromasters": {
        "pipeline": "micromasters-pipeline",
        "repo": "mitodl/micromasters",
        "branch": "master",
    },
    "mit-learn": {
        "pipeline": "mit-learn-pipeline",
        "repo": "mitodl/mit-learn",
        "branch": "main",
    },
    "mit-learn-nextjs": {
        "pipeline": "mit-learn-nextjs-pipeline",
        "repo": "mitodl/mit-learn",
        "branch": "main",
    },
    "mitxonline": {
        "pipeline": "mitxonline-pipeline",
        "repo": "mitodl/mitxonline",
        "branch": "main",
    },
    "ocw-studio": {
        "pipeline": "ocw-studio-pipeline",
        "repo": "mitodl/ocw-studio",
        "branch": "master",
    },
    "odl-video-service": {
        "pipeline": "odl-video-service-pipeline",
        "repo": "mitodl/odl-video-service",
        "branch": "master",
    },
    "xpro": {
        "pipeline": "xpro-pipeline",
        "repo": "mitodl/mitxpro",
        "branch": "master",
    },
}
repos_config = bot_config.get_object("repos_config") or default_repos_config
REPOS_CONFIG = json.dumps(repos_config)

env_suffix_lower = stack_info.env_suffix.lower()
resource_name = f"release-bot-{env_suffix_lower}"
namespace = "operations"

ecr_repository = aws.ecr.Repository(
    f"release-bot-ecr-repository-{stack_info.env_suffix}",
    name=resource_name,
    image_tag_mutability="MUTABLE",
    image_scanning_configuration=aws.ecr.RepositoryImageScanningConfigurationArgs(
        scan_on_push=True,
    ),
    force_delete=True,
    tags=aws_config.tags,
)

ecr_lifecycle_policy = aws.ecr.LifecyclePolicy(
    f"release-bot-ecr-lifecycle-{stack_info.env_suffix}",
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

bot_image_name = ecr_repository.repository_url.apply(lambda url: f"{url}:latest")

secret_resource_name = f"release-bot-secret-{env_suffix_lower}"
bot_secret = kubernetes.core.v1.Secret(
    f"release-bot-secret-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=secret_resource_name,
        namespace=namespace,
    ),
    string_data={
        "slack-bot-token": bot_secrets["slack-bot-token"],
        "slack-app-token": bot_secrets["slack-app-token"],
        "concourse-user": bot_secrets["concourse-username"],
        "concourse-password": bot_secrets["concourse-password"],
        "github-token": github_token,
    },
)


def _secret_env(name: str, key: str) -> kubernetes.core.v1.EnvVarArgs:
    return kubernetes.core.v1.EnvVarArgs(
        name=name,
        value_from=kubernetes.core.v1.EnvVarSourceArgs(
            secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                name=secret_resource_name,
                key=key,
            )
        ),
    )


bot_deployment = kubernetes.apps.v1.Deployment(
    f"release-bot-deployment-{stack_info.env_suffix}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=resource_name,
        namespace=namespace,
        labels={
            "app": resource_name,
            "ou": BusinessUnit.operations,
            "environment": stack_info.env_suffix,
        },
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        # Single replica is intentional: Slack Socket Mode maintains one persistent
        # WebSocket per app-level token. Multiple replicas would each open a competing
        # connection and Slack distributes events non-deterministically across them,
        # meaning some interactions (e.g. button clicks) may silently be dropped by
        # replicas that lack the right Bolt app state context. See the design doc in
        # slack_release_bot_plan.md §"Design constraints".
        replicas=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels={"app": resource_name},
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels={"app": resource_name},
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="release-bot",
                        image=bot_image_name,
                        env=[
                            _secret_env("SLACK_BOT_TOKEN", "slack-bot-token"),
                            _secret_env("SLACK_APP_TOKEN", "slack-app-token"),
                            _secret_env("CONCOURSE_USER", "concourse-user"),
                            _secret_env("CONCOURSE_PASSWORD", "concourse-password"),
                            _secret_env("GITHUB_TOKEN", "github-token"),
                            kubernetes.core.v1.EnvVarArgs(
                                name="CONCOURSE_URL",
                                value=concourse_url,
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="CONCOURSE_TEAM",
                                value="main",
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="REPOS_CONFIG",
                                value=REPOS_CONFIG,
                            ),
                        ],
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={
                                "cpu": "10m",
                                "memory": "128Mi",
                            },
                            limits={
                                "memory": "256Mi",
                            },
                        ),
                    ),
                ],
            ),
        ),
    ),
)

export("ecr_repository_url", ecr_repository.repository_url)
export("bot_image", bot_image_name)
