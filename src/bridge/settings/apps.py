"""Canonical per-application registry: repo, default branch, and notification identity.

Consumed by both the Concourse pipeline generator
(``src/ol_concourse/pipelines/infrastructure/k8s_apps/pipeline.py``) and the
release bot (``src/ol_infrastructure/applications/release_bot/__main__.py``),
so an app's GitHub repo, default branch, and Slack channel are defined in
exactly one place instead of being hand-duplicated (and drifting) across
both control surfaces.

``APPS`` is the canonical enumeration of every app both control surfaces
know about -- the release bot iterates its keys directly
(``for app_name in APPS``) to build its config, so an app must have an entry
here to be picked up at all, even if every field is left at its default.
Only the *field values* default sparsely: leave ``github_repo``/
``repo_main_branch`` unset on an ``AppRegistration()`` when the app's repo is
``mitodl/{app_name}`` on branch ``"main"``; the accessor functions below fill
those in.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class AppRegistration:
    """Canonical metadata for one deployable application.

    :param github_repo: "owner/repo" slug. Defaults to ``mitodl/{app_name}``
        when unset -- only set this when the repo name differs from the app
        name (e.g. app ``xpro`` lives in repo ``mitodl/mitxpro``).
    :param repo_main_branch: The repo's default branch.
    :param slack_channel: Slack channel for release-bot notifications (e.g.
        "ready to promote"), as either a channel id or a channel name. Slack's
        Web API only accepts ids, so a name is resolved through
        conversations.list at runtime (release_bot/slack_channels.py) -- which
        needs groups:read on the bot token and, for a private channel, the bot
        to have been invited to it. Unset means notifications are skipped for
        this app unless RELEASE_ANNOUNCE_CHANNEL provides a fallback.
    """

    github_repo: str | None = None
    repo_main_branch: str = "main"
    slack_channel: str | None = None


APPS: dict[str, AppRegistration] = {
    "learn-ai": AppRegistration(slack_channel="product-learn-ai"),
    "micromasters": AppRegistration(
        repo_main_branch="master", slack_channel="product-micromasters"
    ),
    "mit-learn": AppRegistration(slack_channel="product-mit-learn"),
    # mit-learn-nextjs shares mit-learn's repo *and* Slack channel -- they are
    # independently versioned pipelines/release issues (different issue_prefix,
    # different checklists) that both draw from the same commit history, so
    # the same channel will see two separate "ready to promote" notifications
    # for a change that touches both frontend and backend. Not reconciled
    # into a single release flow here; flagging as a known wrinkle.
    "mit-learn-nextjs": AppRegistration(
        github_repo="mitodl/mit-learn", slack_channel="product-mit-learn"
    ),
    "mitxonline": AppRegistration(slack_channel="product-mitx-online"),
    "ocw-studio": AppRegistration(
        repo_main_branch="master", slack_channel="product-ocw"
    ),
    "odl-video-service": AppRegistration(
        repo_main_branch="master", slack_channel="product-ovs"
    ),
    # No app-specific channel given -- falls back to RELEASE_ANNOUNCE_CHANNEL.
    "ol-analytics-api": AppRegistration(),
    "xpro": AppRegistration(
        github_repo="mitodl/mitxpro",
        repo_main_branch="master",
        slack_channel="product-xpro",
    ),
}


def github_repo(app_name: str) -> str:
    """Return the "owner/repo" slug for the given app."""
    entry = APPS.get(app_name)
    if entry and entry.github_repo:
        return entry.github_repo
    return f"mitodl/{app_name}"


def repo_main_branch(app_name: str) -> str:
    """Return the default branch of the given app's repo."""
    entry = APPS.get(app_name)
    return entry.repo_main_branch if entry else "main"


def slack_channel(app_name: str) -> str | None:
    """Return the configured Slack channel for release notifications, if any."""
    entry = APPS.get(app_name)
    return entry.slack_channel if entry else None


#: GitHub App id for `ol-release-bot`, the identity the Concourse `release`,
#: `github-issues` and `github-deployments` resources authenticate as. This is the
#: APP id (`app_id`), not the installation id -- ruleset bypass actors key on the
#: former. Re-derive with:
#:
#:     gh api /orgs/mitodl/installations \
#:       --jq '.installations[] | select(.app_slug=="ol-release-bot") | .app_id'
#:
#: Lives here rather than in the GitHub Pulumi stacks because both of them need it
#: and neither owns it: it is a fact about the release workflow, which this module
#: is the registry for.
RELEASE_BOT_APP_ID = 4437866


def release_workflow_repos() -> frozenset[str]:
    """Return the "owner/repo" slugs slated for the ol-release-bot release workflow.

    EVERY REGISTERED APP, NOT ONLY THE ONES ALREADY RUNNING THE NEW WORKFLOW. That
    is deliberate and it over-grants on purpose (decision: Tobias, 2026-09-01). The
    actual opt-in is ``AppPipelineParams.use_release_resource_workflow`` in
    ``src/ol_concourse/pipelines/infrastructure/k8s_apps/pipeline.py``, which
    defaults to ``False`` and today is set on ``ol-analytics-api`` alone -- so the
    consumer below grants a bypass on ``mit-learn`` and ``mitxonline`` for an App
    that does not yet finish their releases.

    The trade is pre-provisioning against a rollout that would otherwise need a
    GitHub ruleset edit interleaved with every pipeline flip -- and that edit is the
    step that already went wrong once: ol-analytics-api's ``releases/2026.8.28.2``
    shipped to production and then sat unmerged because the bypass was missing at
    exactly this moment. Granting ahead means flipping a pipeline is a one-line
    change with no privileged GitHub operation behind it.

    What it costs is bounded: the bypass only exists where a repo also declares
    ``required_status_checks``, and the App still cannot act on a repo it is not
    installed on. Narrow this to opted-in apps once the rollout finishes -- by then
    the two sets are the same and the over-grant is free to drop.

    Deduplicated, because two app entries can share one repo -- ``mit-learn`` and
    ``mit-learn-nextjs`` are independently versioned pipelines over the same
    ``mitodl/mit-learn`` history.
    """
    return frozenset(github_repo(app_name) for app_name in APPS)
