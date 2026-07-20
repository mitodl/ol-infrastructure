"""
Pulumi stack to deploy the release bot.

This stack:
1. Deploys the release bot to Kubernetes (image built and pushed to ECR
   by the Concourse simple_pulumi pipeline, including the ECR repository
   itself)
2. Creates necessary secrets

The bot uses Slack Socket Mode (outbound WebSocket) — no Service or Ingress required.
Single replica: Socket Mode connections are not multiplexed.
"""

import json
from pathlib import Path

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pulumi import Config, export, log

from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit, Environment
from ol_infrastructure.lib.pulumi_helper import (
    format_docker_image_ref,
    make_stack_reference,
    parse_stack,
)

stack_info = parse_stack()
log.info(f"{stack_info=}")

aws_config = AWSBase(
    tags={
        "OU": BusinessUnit.operations,
        "Environment": Environment.operations,
    },
)

# Singleton service (stack name "default") -- always deployed against the
# applications cluster's Production stage, so the target is hardcoded rather
# than derived from stack_info.
cluster_stack = make_stack_reference(projects.EKS, "applications.Production")
setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

bot_secrets = read_yaml_secrets(Path("release_bot/secrets.production.yaml"))
concourse_ops_secrets = read_yaml_secrets(Path("concourse/operations.production.yaml"))
github_token = concourse_ops_secrets["pipelines"]["infrastructure/github"][
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
    "ol-analytics-api": {
        "pipeline": "ol-analytics-api-pipeline",
        "repo": "mitodl/ol-analytics-api",
        "branch": "main",
    },
    "xpro": {
        "pipeline": "xpro-pipeline",
        "repo": "mitodl/mitxpro",
        "branch": "master",
    },
}
repos_config = bot_config.get_object("repos_config") or default_repos_config
REPOS_CONFIG = json.dumps(repos_config)

resource_name = "release-bot-production"
namespace = "operations"

# The ECR repository itself is created (idempotently) by the Concourse
# simple_pulumi image-build job on push, not managed here. The image is
# pinned by digest (RELEASE_BOT_DOCKER_SHA, set by the build job) rather
# than the mutable "latest" tag, so a new push actually changes this
# Deployment's pod spec and triggers a rollout instead of silently leaving
# the running pod on a stale image.
aws_account = aws.get_caller_identity()
image_repository = (
    f"{aws_account.account_id}.dkr.ecr.{aws_config.region}.amazonaws.com"
    f"/{resource_name}"
)
bot_image_name = format_docker_image_ref(image_repository, "RELEASE_BOT")

secret_resource_name = (
    "release-bot-secret-production"  # pragma: allowlist secret  # noqa: S105
)
bot_secret = kubernetes.core.v1.Secret(
    "release-bot-secret-production",  # pragma: allowlist secret
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
    "release-bot-deployment-production",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=resource_name,
        namespace=namespace,
        labels={
            "app": resource_name,
            "ou": BusinessUnit.operations,
            "environment": "production",
        },
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        # Single replica is intentional, not an oversight: Slack Socket Mode maintains
        # one persistent WebSocket per app-level token. Running 2+ replicas (the
        # original design target in #4485) would have each replica open a competing
        # connection, and Slack distributes events non-deterministically across them,
        # silently dropping interactions (e.g. button clicks) on whichever replica
        # lacks the Bolt app state for that interaction. Scaling this out safely would
        # require either leader-election so only one replica holds the live socket, or
        # moving off Socket Mode to Slack's HTTP Events API (which needs a public
        # ingress). Neither is implemented; #4485's acceptance criteria have been
        # updated to reflect single-replica as the accepted tradeoff for this phase.
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
                                value="infrastructure",
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

export("bot_image", bot_image_name)
