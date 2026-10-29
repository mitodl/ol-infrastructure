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

import hashlib
import json
import re
from pathlib import Path

import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
from pulumi import Config, export, log

from bridge.secrets.sops import read_yaml_secrets
from bridge.settings.apps import APPS
from bridge.settings.apps import github_repo as app_github_repo
from bridge.settings.apps import (
    release_resource_workflow as app_release_resource_workflow,
)
from bridge.settings.apps import repo_main_branch as app_repo_main_branch
from bridge.settings.apps import slack_channel as app_slack_channel
from bridge.settings.libraries import LIBRARIES, UNPUBLISHABLE_LIBRARIES
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
# Same GitHub App installation Concourse's github-issues resource type can use
# via auth_method="app" (ol_concourse.lib.resources.github_issues) -- sharing
# one App instead of each consumer holding its own long-lived PAT. Backed by
# the "shared/github_app" entry, the same Vault-synced secret k8s_apps/
# pipeline.py references as ((github_app.release_bot_app_id)) etc. Named
# "github_app" rather than "github" because every Concourse team already has
# its own team-scoped "github" secret (e.g. secret-concourse/infrastructure/
# github) -- Concourse's Vault credential manager tries team-scoped paths
# before the shared fallback, so "github" alone would resolve to the wrong,
# fieldless secret for any pipeline in a team that already has one.
github_secrets = concourse_ops_secrets["pipelines"]["shared/github_app"]
github_app_id = github_secrets["release_bot_app_id"]
github_app_installation_id = github_secrets["release_bot_app_installation_id"]
github_app_private_key = github_secrets["release_bot_app_pem"]

bot_config = Config("release_bot")
concourse_url = bot_config.get("concourse_url") or "https://cicd.odl.mit.edu"
# Fallback Slack channel for "ready to promote" notifications when an app
# doesn't set its own `channel` in repos_config (e.g. ol-analytics-api,
# which has no dedicated product channel yet).
release_announce_channel = (
    bot_config.get("release_announce_channel") or "product-infrastructure"
)
# How long a release may sit cut-but-unfinished before the bot reports it in
# Slack. Left unset the bot uses 16h; see _DEFAULT_STUCK_AFTER_HOURS in bot.py.
release_stuck_after_hours = bot_config.get("release_stuck_after_hours")

# Derived from bridge.settings.apps -- the shared app registry also used by
# the Concourse pipeline generator (k8s_apps/pipeline.py) -- so an app's repo,
# branch, and Slack channel are defined in exactly one place. The Pulumi
# config override below still lets a specific stack diverge if it ever needs
# to (e.g. testing against a fork).
default_repos_config = {
    app_name: {
        "pipeline": f"{app_name}-pipeline",
        "repo": app_github_repo(app_name),
        "branch": app_repo_main_branch(app_name),
        # Apps still on the legacy release-candidate/release pipeline are
        # published here too, and the bot refuses their release commands
        # rather than dropping them from its vocabulary: "still on the legacy
        # pipeline" is the answer someone typing `/doof release <app>` needs,
        # and it is also what makes the rollout a one-field change in
        # bridge.settings.apps.
        "release_workflow": app_release_resource_workflow(app_name),
        **(
            {"channel": app_slack_channel(app_name)}
            if app_slack_channel(app_name)
            else {}
        ),
    }
    for app_name in APPS
}
repos_config = bot_config.get_object("repos_config") or default_repos_config
REPOS_CONFIG = json.dumps(repos_config)

# Publishable libraries, from the registry that is their single source of
# truth. Separate from repos_config because a library is published rather than
# released: no RC/Production split, no release issue, no checklist gate -- and
# critically, a different Concourse team (`main`, not the `infrastructure`
# team CONCOURSE_TEAM names below), which is why `/doof publish` could not have
# worked against an app-shaped config no matter what job it asked for.
LIBRARIES_CONFIG = json.dumps(
    {
        name: {
            "pipeline": lib.pipeline,
            "team": lib.team,
            "publish_job": lib.publish_job,
            "package_job_prefix": lib.package_job_prefix,
            "github_repo": lib.github_repo,
            "registry": lib.registry,
        }
        for name, lib in LIBRARIES.items()
    }
)
# Libraries Doof could publish that have no Concourse pipeline, mapped to why,
# so the bot answers a request for one instead of calling it unknown.
UNPUBLISHABLE_LIBRARIES_CONFIG = json.dumps(UNPUBLISHABLE_LIBRARIES)

# Commit identity -> Slack member id, for the authors `users.lookupByEmail`
# cannot find because they commit under a GitHub no-reply or personal address
# rather than their Slack one (slack_users.py). Kept in the SOPS secret rather
# than bridge.settings for two reasons: this repository is public and the
# mapping is a roster of people, and SOPS encrypts values but NOT keys -- so it
# is stored as one JSON string under a single key, encrypted whole, instead of
# a YAML mapping that would leave every GitHub handle in plaintext.
#
# Read with `.get`, and optional again in bot_config, so a bot image newer than
# the secret (or older than this stack) still boots and resolves authors
# exactly as it did before any of this existed.
slack_user_overrides = bot_secrets.get("slack-user-overrides") or ""
if slack_user_overrides:
    # Fail at `pulumi up` rather than shipping a pod that logs an error and
    # quietly goes back to notifying nobody. The runtime loader degrades to "no
    # overrides" on anything malformed, which is the right behaviour for a
    # running bot and the wrong one for a deploy: checking the same three
    # things here (object, non-empty keys, member-id-shaped values) means the
    # realistic edits -- a stray list, a display name pasted into a value --
    # are caught by the person making them. The regex is the one in
    # slack_users._SLACK_USER_ID_RE; keep them in step.
    _overrides = json.loads(slack_user_overrides)
    if not isinstance(_overrides, dict):
        msg = (
            "slack-user-overrides must be a JSON object of identity -> Slack "
            f"member id, got {type(_overrides).__name__}"
        )
        raise TypeError(msg)

    # Identities are truncated at the first "@" in any message raised below, the
    # way slack_users._fallback redacts them: `pulumi up` for this stack can run
    # in Concourse, whose build log is readable more widely than the secret is,
    # and a commit address is enough to identify someone. A handle (no "@")
    # survives whole, which is the common case and is public anyway.
    def _redact(identity: str) -> str:
        return identity.split("@", 1)[0] or "(blank)"

    _bad = sorted(
        _redact(identity)
        for identity, user_id in _overrides.items()
        if not identity.strip()
        or not re.match(r"^[UW][A-Z0-9]{7,}$", str(user_id).strip().lstrip("@").upper())
    )
    if _bad:
        msg = (
            "slack-user-overrides entries must map an identity to a Slack member "
            f"id (U… or W…), not a display name, handle, or email: {', '.join(_bad)}"
        )
        raise ValueError(msg)
    # Keys are lowercased at load, so two that differ only in case collapse into
    # one and the survivor depends on dict order -- silently dropping whichever
    # entry lost. Cheaper to refuse the file than to debug that later.
    _collisions = sorted(
        {
            _redact(identity.strip().lower())
            for identity in _overrides
            if sum(
                other.strip().lower() == identity.strip().lower()
                for other in _overrides
            )
            > 1
        }
    )
    if _collisions:
        msg = (
            "slack-user-overrides has entries differing only by case, which "
            f"collapse into one at load: {', '.join(_collisions)}"
        )
        raise ValueError(msg)

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

secret_resource_name = "release-bot-secret-production"  # pragma: allowlist secret  # noqa: S105
bot_secret_data = {
    # The bot token needs "users:read.email" on top of the usual
    # chat/commands scopes: slack_users.py resolves each commit author's
    # email to a Slack id so release notifications @-mention real people
    # instead of pasting addresses. Without the scope the bot logs one
    # error and degrades to plain-text names. "users:read" is wanted on top
    # of that, for the boot-time check that every slack-user-overrides
    # entry still points at a live account.
    "slack-bot-token": bot_secrets["slack-bot-token"],
    "slack-app-token": bot_secrets["slack-app-token"],
    "slack-user-overrides": slack_user_overrides,
    "concourse-user": bot_secrets["concourse-username"],
    "concourse-password": bot_secrets["concourse-password"],
    "github-app-id": str(github_app_id),
    "github-app-installation-id": str(github_app_installation_id),
    "github-app-private-key": github_app_private_key,
}
bot_secret = kubernetes.core.v1.Secret(
    "release-bot-secret-production",  # pragma: allowlist secret
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=secret_resource_name,
        namespace=namespace,
    ),
    string_data=bot_secret_data,
)
# Adding a key here replaces the Secret rather than updating it, so a preview
# reports `creation failed: secrets "release-bot-secret-production" already
# exists`. That is the provider dry-running the create while the original is
# still there, not a blocked update: the name is pinned, so the provider
# deletes before replacing. #5169 added the three github-app-* keys the same
# way and shipped.
#
# A running pod does not notice the swap: env vars are read once at container
# start, and the Deployment names this Secret by a fixed string, so changing a
# value -- an edit to slack-user-overrides, a token rotation -- replaces the
# Secret and leaves the pod template untouched. `pulumi up` would report
# success and the bot would keep the old values until something else restarted
# it. Hashing the contents into a pod-template annotation makes any change here
# roll the pod, the same way the checksum/ol-* annotations in the dagster stack
# do. Hashed together rather than per key: an annotation is readable by anyone
# who can read the Deployment, and the high-entropy tokens keep the digest from
# being a fingerprint someone could test a guessed override list against.
bot_secret_checksum_annotation = {
    "checksum/release-bot-secret": hashlib.sha256(
        json.dumps(bot_secret_data, sort_keys=True).encode()
    ).hexdigest(),
}


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
                annotations=bot_secret_checksum_annotation,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="release-bot",
                        image=bot_image_name,
                        env=[
                            _secret_env("SLACK_BOT_TOKEN", "slack-bot-token"),
                            _secret_env("SLACK_APP_TOKEN", "slack-app-token"),
                            _secret_env("SLACK_USER_OVERRIDES", "slack-user-overrides"),
                            _secret_env("CONCOURSE_USER", "concourse-user"),
                            _secret_env("CONCOURSE_PASSWORD", "concourse-password"),
                            _secret_env("GITHUB_APP_ID", "github-app-id"),
                            _secret_env(
                                "GITHUB_APP_INSTALLATION_ID",
                                "github-app-installation-id",
                            ),
                            _secret_env(
                                "GITHUB_APP_PRIVATE_KEY", "github-app-private-key"
                            ),
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
                            kubernetes.core.v1.EnvVarArgs(
                                name="LIBRARIES_CONFIG",
                                value=LIBRARIES_CONFIG,
                            ),
                            kubernetes.core.v1.EnvVarArgs(
                                name="UNPUBLISHABLE_LIBRARIES",
                                value=UNPUBLISHABLE_LIBRARIES_CONFIG,
                            ),
                            *(
                                [
                                    kubernetes.core.v1.EnvVarArgs(
                                        name="RELEASE_ANNOUNCE_CHANNEL",
                                        value=release_announce_channel,
                                    )
                                ]
                                if release_announce_channel
                                else []
                            ),
                            *(
                                [
                                    kubernetes.core.v1.EnvVarArgs(
                                        name="RELEASE_STUCK_AFTER_HOURS",
                                        value=release_stuck_after_hours,
                                    )
                                ]
                                if release_stuck_after_hours
                                else []
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
