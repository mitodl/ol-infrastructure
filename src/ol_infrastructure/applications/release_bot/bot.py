"""Slack Bolt async app — all command and action handlers for the release bot."""

import asyncio
import logging
import os
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from functools import partial
from typing import Any
from zoneinfo import ZoneInfo

import bot_config as config
import concourse_client as concourse
import github_client as github
import slack_channels
import slack_users
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

log = logging.getLogger(__name__)

# Set by create_app(). Background tasks started from a slash command need to
# post to a configured channel rather than reply to the command, because a
# checklist watch outlives the 30-minute window a `respond` URL stays valid.
# A module global is safe here: Socket Mode connections are not multiplexed,
# so this process runs exactly one AsyncApp (see __main__.py's replicas=1).
_slack_app: AsyncApp | None = None

# Slack's ack and respond callables as the handlers receive them; respond may
# be the _in_channel wrapper rather than slack_bolt's own AsyncRespond.
_SlackCallable = Callable[..., Awaitable[Any]]


# app name -> Slack user id of whoever last ran `/doof release` for it, so the
# ready-to-promote notification can ping the person who owns the release
# rather than posting into the channel addressed to no one (Doof pinged "the
# Merginator" there). Process-local and deliberately not persisted: a bot
# restart costs one mention, and the message is still posted either way.
_release_requesters: dict[str, str] = {}


# Publishable libraries, and the libraries Doof could publish that have no
# Concourse pipeline. Set by create_app() from the environment. Module globals
# for the same reason as _slack_app: one process, one AsyncApp. They are not
# threaded through the handler signature because `publish` is the only
# subcommand that has anything to do with libraries -- every other one is
# about apps and takes `repos`.
_libraries: dict[str, "config.LibraryConfig"] = {}
_unpublishable_libraries: dict[str, str] = {}


# app name -> lock held across a command's check-then-act sequence. Slack
# handlers run concurrently on one event loop, so without it two commands can
# each read "nothing pending" before either writes: two hotfix requests for
# one app, or a release triggered while a hotfix is being requested.
# Process-local is enough because the bot runs one replica (see _slack_app).
_app_locks: dict[str, asyncio.Lock] = {}


def _app_lock(app_name: str) -> asyncio.Lock:
    return _app_locks.setdefault(app_name, asyncio.Lock())


# When this process started, for `/doof uptime`. Module import is process
# start for all practical purposes: bot.py is the container's entrypoint.
_started_at = datetime.now(tz=UTC)

# poller name -> (when its last iteration finished, whether every step in it
# succeeded). Written by the background loops and read by `/doof uptime`.
# A loop wedged inside an await stops updating its entry, which is exactly
# the silent failure this exists to make visible.
_poll_heartbeats: dict[str, tuple[datetime, bool]] = {}
_READY_TO_PROMOTE_POLLER = "ready-to-promote"
_RELEASE_PROGRESS_POLLER = "release-progress"


# subcommand -> (its arguments, what it does). Both `/doof help` and the
# per-command usage errors are rendered from this, so they cannot drift.
_SYNTAX: dict[str, tuple[str, str]] = {
    "release": (
        "<app>",
        "cut a release (to replace one already in flight, add `supersede <version>`)",
    ),
    "hotfix": ("<app> <sha>", "cut what production runs plus one commit"),
    "preview": (
        "<app>",
        "show what the next release would contain (changes nothing)",
    ),
    "release-notes": ("<app>", "show unreleased commits"),
    "release-status": ("[app]", "check release issue status"),
    "wait-for-checkboxes": (
        "<app>",
        "watch a release checklist and report progress",
    ),
    "promote": ("<app>", "promote to production"),
    "publish": (
        "<library> [package]",
        "publish a library to PyPI or npm (no argument lists them)",
    ),
    "abandon": ("<app>", "abandon an in-progress release"),
    "version": ("[app]", "show what RC and Production are running"),
    "uptime": ("", "check the bot is alive and its pollers are running"),
    "help": ("", "show this message"),
}


def _syntax(subcommand: str) -> str:
    """Return a subcommand's invocation, e.g. `/doof release <app>`."""
    arguments = _SYNTAX[subcommand][0]
    return f"/doof {subcommand} {arguments}".rstrip()


_USAGE = "Usage:\n" + "".join(
    f"• `{_syntax(subcommand)}` — {what}\n" for subcommand, (_, what) in _SYNTAX.items()
)

# Stands in for <sha> in usage examples. Any 7+ hex digits read as a SHA.
_EXAMPLE_SHA = "1a2b3c4"


def _describe_in_flight(in_flight: dict[str, Any]) -> str:
    """Render an in-flight release as a one-line Slack summary."""
    cut_at = in_flight.get("cut_at")
    when = ""
    if cut_at is not None:
        days = (datetime.now(tz=UTC) - cut_at).days
        when = " (cut today)" if days == 0 else f" (cut {days}d ago)"
    return f"<{in_flight['url']}|{in_flight['version']}>{when}"


def _slack_escape(text: str) -> str:
    """Escape the three characters Slack mrkdwn treats as control syntax."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _commit_line(repo_slug: str, commit: dict[str, Any]) -> str:
    """Render one unreleased commit as a Slack bullet.

    Shared by `/doof preview` and `/doof release-notes` so the two cannot
    disagree about the same release. A commit from a PR links the PR by its
    full URL (the RFC asked for links that resolve, not bare `#123`) under the
    PR's title; any other commit links itself by short SHA.
    """
    number, title = github.pull_request(commit["message"])
    if number is not None:
        ref = f"<https://github.com/{repo_slug}/pull/{number}|#{number}>"
    elif commit.get("url"):
        ref = f"<{commit['url']}|{commit['sha'][:8]}>"
    else:
        ref = f"`{commit['sha'][:8]}`"
    return f"• {ref} {_slack_escape(title)} — {_slack_escape(commit['author'])}"


def _omitted_note(omitted: int | None) -> str | None:
    """Say that a commit list was cut short, or return None if it was not."""
    if omitted is None:
        return (
            "_Only the most recent commits are shown — there is no earlier "
            "release to count from._"
        )
    if omitted:
        # The compare API lists oldest first, so the cap drops the newest.
        return f"_…and {omitted} newer commit(s) not shown._"
    return None


def _app_lists(repos: dict[str, config.AppConfig]) -> str:
    """Name the apps the bot drives, and separately the ones Doof still does."""
    migrated = list(config.release_workflow_apps(repos))
    legacy = [name for name, cfg in repos.items() if not cfg.release_workflow]
    text = f"Apps: {', '.join(migrated) or 'none yet'}"
    if legacy:
        text += f"\nStill on the legacy pipeline, released by Doof: {', '.join(legacy)}"
    return text


def _usage_hint(repos: dict[str, config.AppConfig], subcommand: str) -> str:
    """Return a subcommand's usage line plus an example naming a real app."""
    hint = f"Usage: `{_syntax(subcommand)}`"
    # An app the bot can drive, so copying the example does something.
    example_app = next(iter(config.release_workflow_apps(repos)), None)
    if example_app is not None:
        example = (
            _syntax(subcommand)
            .replace("<app>", example_app)
            .replace("[app]", example_app)
            .replace("<sha>", _EXAMPLE_SHA)
        )
        hint += f", e.g. `{example}`"
    return hint


async def _refuse(respond: _SlackCallable, message: str) -> None:
    """Reply to a command that did nothing, visibly only to whoever ran it.

    _in_channel makes release state visible to the whole channel. A mistyped
    or refused command is not release state, so it does not need an audience.
    """
    await respond(message, response_type="ephemeral")


def _resolve_app(repos, app_name, subcommand=None):
    """Return (cfg, None) for an app this bot can drive, else (None, error).

    `subcommand` names the command being run, so a missing or mistyped app
    gets that command's usage line. The promote button passes none: its app
    comes from the bot's own message, not from anything a person typed.

    Refusing a legacy-pipeline app here is the whole point. Every registered
    app is offered in the command surface, but the jobs and artifacts these
    handlers reach for -- `build-<app>-release-image`, `<app>-release-gate`,
    the `releases/<version>` branch, the release issue, the calver tags
    `release_preview` numbers the next version from -- exist only in the
    modernized pipeline shape. Against a legacy app the mutating commands 404
    in Concourse with no hint as to why, and the read-only ones are worse:
    they return confidently wrong answers, e.g. `/doof preview` names a calver
    version the legacy pipeline will never cut.
    """
    if app_name not in repos:
        if app_name or subcommand is None:
            problem = f"Unknown app `{_slack_escape(app_name)}`."
        else:
            problem = f"`/doof {subcommand}` needs an app."
        hint = f"\n{_usage_hint(repos, subcommand)}" if subcommand else ""
        return None, f"{problem}{hint}\n{_app_lists(repos)}"
    cfg = repos[app_name]
    if not cfg.release_workflow:
        return None, (
            f"`{app_name}` is still on the legacy release-candidate/release "
            "pipeline, which this bot does not drive. Doof still owns its "
            "releases until the pipeline is migrated."
        )
    return cfg, None


# The word that confirms discarding an in-flight release: `/doof release <app>
# supersede <version>`. A plain word, not a `--flag`, because Slack clients may
# rewrite a leading `--` to a dash. The version is the release the person was
# warned about, so a confirmation cannot be applied to a different release cut
# in the meantime.
_SUPERSEDE = "supersede"


async def _cmd_release(repos, ack, respond, command, context):
    await ack()
    parts = command["text"].split()
    app_name, options = (parts[0], parts[1:]) if parts else ("", [])
    cfg, error = _resolve_app(repos, app_name, "release")
    if cfg is None:
        await _refuse(respond, error)
        return
    confirmed_version = None
    if options:
        if len(options) != 2 or options[0].lower() != _SUPERSEDE:  # noqa: PLR2004
            await _refuse(
                respond,
                f"Unrecognised option `{_slack_escape(' '.join(options))}`. "
                f"{_usage_hint(repos, 'release')}\n"
                f"To replace a release that is already in flight, add "
                f"`{_SUPERSEDE} <version>` naming it.",
            )
            return
        confirmed_version = options[1]
    async with _app_lock(app_name):
        await _release(respond, app_name, cfg, context, supersede=confirmed_version)


async def _guard_release_start(
    respond, app_name, cfg, supersede: str | None
) -> tuple[bool, dict[str, Any] | None]:
    """Decide whether `/doof release` may go ahead.

    Returns (proceed, in_flight). When not proceeding, the person has already
    been told why.
    """
    # A release that was cut but never finished. Cutting a new one supersedes
    # it on action=create, deleting its branch (and possibly its tag). Doof
    # refused outright and people type `/doof release` twice by reflex, so
    # that is only done when asked for.
    try:
        in_flight = await github.in_flight_release(cfg.repo)
    except Exception:
        log.exception("Failed to check in-flight release for %s", app_name)
        await _refuse(
            respond,
            f"❌ Could not check whether `{app_name}` has a release in flight. "
            "Refusing to trigger — a new release would supersede it, deleting "
            "its branch, without asking.",
        )
        return False, None
    if in_flight and supersede != in_flight["version"]:
        version = in_flight["version"]
        changed = (
            f"You confirmed `{_slack_escape(supersede)}`, but that is not what is "
            "in flight now. "
            if supersede is not None
            else ""
        )
        await _refuse(
            respond,
            f"🚧 {changed}`{app_name}` already has release "
            f"{_describe_in_flight(in_flight)} in flight. A new release supersedes "
            "it: its release branch is deleted, and its tag too if it never "
            "reached production.\n"
            f"• `/doof release {app_name} {_SUPERSEDE} {version}` discards it and "
            "cuts a new release\n"
            f"• `/doof release-status {app_name}` shows where it stands\n"
            f"• `/doof abandon {app_name}` drops it without a new release",
        )
        return False, None
    # The release branch only appears when the build reaches `action: create`,
    # so GitHub cannot see a release that has been triggered but not cut. A
    # second `/doof release` typed right after the first finds nothing in
    # flight, and a second `supersede <version>` still finds the release the
    # first one is replacing; either way its create supersedes the first
    # build's cut without asking, so this is checked after a confirmation too.
    # `_app_lock` serializes the commands, and the first one's build exists
    # (pending) before the lock is released, so the second one sees it.
    job = f"build-{app_name}-release-image"
    try:
        running = await concourse.active_build_url(cfg.pipeline, job)
    except Exception:
        log.exception("Failed to check for a running release build: %s", app_name)
        await _refuse(
            respond,
            f"❌ Could not check whether a release build for `{app_name}` is "
            "already running. Refusing to trigger — a second one would "
            "supersede whatever the first one cuts.",
        )
        return False, None
    if running:
        await _refuse(
            respond,
            f"🚧 A release build for `{app_name}` is already running: "
            f"{running}. A release started now would supersede whatever it cuts, "
            "without asking. Try again once it finishes (see "
            f"`/doof release-status {app_name}`), or abort the build in "
            "Concourse if it is stuck.",
        )
        return False, None
    return True, in_flight


async def _release(respond, app_name, cfg, context, *, supersede: str | None = None):
    """Trigger a release.

    `supersede` is the version of the in-flight release the caller confirmed
    discarding; it only counts if that is still the release in flight.
    """
    # While a hotfix request is pending the release resource offers that
    # hotfix, not the tracked branch, so this build would cut it instead.
    try:
        pending = await github.pending_hotfix_requests(cfg.repo)
    except Exception:
        log.exception("Failed to check pending hotfixes for %s", app_name)
        await respond(
            f"❌ Could not check `{app_name}` for a pending hotfix. Refusing to "
            "trigger — a pending one would be cut instead of this release."
        )
        return
    if pending:
        short = pending[0][:7]
        await respond(
            f"A hotfix of `{short}` is pending for `{app_name}`, so a release "
            f"now would cut that hotfix instead. `/doof hotfix {app_name} {short}` "
            f"continues it; `/doof abandon {app_name}` cancels it."
        )
        return

    proceed, in_flight = await _guard_release_start(respond, app_name, cfg, supersede)
    if not proceed:
        return

    try:
        # The release resource is configured `check_every: never` with no
        # webhook, so Concourse only checks it when explicitly asked.
        # Triggering the build job without this makes the job bind whatever
        # version Concourse last recorded -- the *previous* release -- so the
        # new release reuses its version number and its commit list.
        await concourse.check_resource(cfg.pipeline, f"{app_name}-release")
    except Exception:
        log.exception("Failed to force release resource check for %s", app_name)
        await respond(
            f"❌ Could not refresh the release version for `{app_name}`. "
            "Refusing to trigger — the build would have reused the previous "
            "release's version and commits."
        )
        return

    try:
        # Matches the release-image build job name generated by
        # k8s_apps/pipeline.py's release-resource workflow
        # (_build_release_resource_app_pipeline / _build_release_image_job).
        build_url = await concourse.trigger_job(
            cfg.pipeline, f"build-{app_name}-release-image"
        )
    except Exception:
        log.exception("Failed to trigger release for %s", app_name)
        await respond(f"❌ Failed to trigger release for `{app_name}`. Check logs.")
        return

    requester = (context or {}).get("user_id")
    if requester:
        _release_requesters[app_name] = requester

    message = f"🚀 Release triggered for `{app_name}`. Build: {build_url}"
    if in_flight:
        # The tag is only deleted when that release never reached production;
        # one whose deploy succeeded and whose `finish` failed keeps its tag,
        # since it is the only marker of what production is running.
        message += (
            f"\n⚠️ Superseding in-flight release {_describe_in_flight(in_flight)} — "
            "its release branch will be deleted, and its tag too if it never "
            "reached production."
        )
    await respond(message)


async def _cmd_preview(repos, ack, respond, command, _context):
    """Show what the next release would contain, without triggering anything."""
    await ack()
    app_name = command["text"].strip()
    cfg, error = _resolve_app(repos, app_name, "preview")
    if cfg is None:
        await _refuse(respond, error)
        return
    # While a hotfix request is pending the release resource offers that
    # hotfix, not the tracked branch's calver-next version -- same check
    # `_release` uses, so the preview doesn't contradict what `/doof release`
    # would actually cut.
    try:
        pending = await github.pending_hotfix_requests(cfg.repo)
    except Exception:
        log.exception("Failed to check pending hotfixes for %s", app_name)
        await respond(f"❌ Could not check `{app_name}` for a pending hotfix.")
        return
    if pending:
        short = pending[0][:7]
        await respond(
            f"*Release preview for `{app_name}`* — a hotfix of `{short}` is "
            "pending, so the next cut would be that hotfix (what production "
            "runs plus that commit), not a normal release.\n"
            f"`/doof hotfix {app_name} {short}` continues it; "
            f"`/doof abandon {app_name}` cancels it."
        )
        return
    try:
        preview = await github.release_preview(cfg.repo)
    except Exception:
        log.exception("Failed to build release preview for %s", app_name)
        await respond(f"❌ Failed to preview a release for `{app_name}`.")
        return

    commits = preview["commits"]
    omitted = preview["omitted"]
    since = preview["since"] or "the start of history"
    # A truncated list under-reports, so say "at least" rather than a count
    # that reads as the whole release.
    count = f"At least {len(commits)}" if omitted != 0 else str(len(commits))
    lines = [
        f"*Release preview for `{app_name}`* — next version would be "
        f"`{preview['version']}`",
        f"_{count} commit(s) since {since}_",
    ]
    if preview["in_flight"]:
        lines.append(
            f"⚠️ In flight: {_describe_in_flight(preview['in_flight'])} — cut but "
            "not finished. Cutting a new release supersedes it, and "
            f"`/doof release {app_name} {_SUPERSEDE} "
            f"{preview['in_flight']['version']}` is how you ask for that."
        )
    if commits:
        lines += [_commit_line(cfg.repo, c) for c in commits]
    else:
        lines.append("_Nothing to release._")
    if note := _omitted_note(omitted):
        lines.append(note)
    lines.append("_Preview only — nothing was triggered._")
    await respond("\n".join(lines))


async def _cmd_release_notes(repos, ack, respond, command, _context):
    await ack()
    app_name = command["text"].strip()
    cfg, error = _resolve_app(repos, app_name, "release-notes")
    if cfg is None:
        await _refuse(respond, error)
        return
    try:
        unreleased = await github.commits_since_last_tag(cfg.repo)
    except Exception:
        log.exception("Failed to fetch release notes for %s", app_name)
        await respond(f"❌ Failed to fetch release notes for `{app_name}`.")
        return
    commits = unreleased["commits"]
    if not commits:
        await respond(
            f"*Release notes for `{app_name}`*\n_(no commits since last release)_"
        )
        return
    lines = [_commit_line(cfg.repo, c) for c in commits]
    if note := _omitted_note(unreleased["omitted"]):
        lines.append(note)
    await respond(f"*Release notes for `{app_name}`*\n" + "\n".join(lines))


async def _in_flight_status(name, cfg, *, has_open_issue: bool) -> str:
    """Render the in-flight release suffix of a release-status line, or ""."""
    # A release branch outliving its production deploy means the finish
    # step never completed. That used to be invisible -- and it froze the
    # release version until someone noticed -- so report it here.
    try:
        in_flight = await github.in_flight_release(cfg.repo)
    except Exception:
        log.exception("Failed to fetch in-flight release for %s", name)
        return ""
    if not in_flight:
        return ""
    status = f" · 🚧 in flight: {_describe_in_flight(in_flight)}"
    # Promotion closes the issue, so a release stuck after production has no
    # open one linked earlier in the line.
    if not has_open_issue and (
        issue := await _find_release_issue(name, cfg, in_flight["version"])
    ):
        status += f" <{issue['url']}|Release issue>{_release_state_suffix(issue)}"
    return status


# How release-status describes each label-derived state of a release issue.
# `release` says nothing the line does not already say, so it is not shown.
_RELEASE_STATE_TEXT = {
    "abandoned": "abandoned",
    "refused": "⛔ refused by Production",
    "consumed": "taken by Production",
    "promote-ready": "ready to promote",
}


def _release_state_suffix(issue: dict[str, Any]) -> str:
    """Render what the issue's labels say happened to it, or ""."""
    state = github.release_state(issue.get("labels", []))
    text = _RELEASE_STATE_TEXT.get(state or "")
    return f" · {text}" if text else ""


async def _cmd_release_status(repos, ack, respond, command, _context):
    await ack()
    app_filter = command["text"].strip() or None
    if app_filter:
        cfg, error = _resolve_app(repos, app_filter, "release-status")
        if cfg is None:
            await _refuse(respond, error)
            return
        targets = {app_filter: cfg}
    else:
        targets = config.release_workflow_apps(repos)
    legacy = [name for name, cfg in repos.items() if not cfg.release_workflow]
    if not targets:
        await respond(
            "No apps are on the release-resource workflow yet, so there is no "
            f"release status to report. Still on the legacy pipeline: "
            f"{', '.join(legacy)}."
        )
        return
    lines = []
    for name, cfg in targets.items():
        try:
            issues = await github.open_release_issues(cfg.repo)
        except Exception:
            log.exception("Failed to fetch release status for %s", name)
            lines.append(f"• *{name}*: ⚠️ error fetching status")
            continue
        if issues:
            checked, total = github.checklist_status(issues[0]["body"])
            title = _slack_escape(issues[0]["title"])
            status = f"🟡 <{issues[0]['url']}|{title}> ({checked}/{total} checked)"
            status += _release_state_suffix(issues[0])
        else:
            status = "✅ no open release"
        status += await _in_flight_status(name, cfg, has_open_issue=bool(issues))
        lines.append(f"• *{name}*: {status}")
    if legacy and not app_filter:
        # Naming them keeps a silently short list from reading as "everything
        # is quiet" during the rollout, when most apps are still on Doof.
        lines.append(
            f"_Not shown -- still on the legacy pipeline: {', '.join(legacy)}._"
        )
    await respond("\n".join(lines))


async def _cmd_promote(repos, ack, respond, command, context):
    await ack()
    app_name = command["text"].strip()
    cfg, error = _resolve_app(repos, app_name, "promote")
    if cfg is None:
        await _refuse(respond, error)
        return
    try:
        issues = await github.open_release_issues(cfg.repo)
    except Exception:
        log.exception("Failed to fetch release issues for %s", app_name)
        await respond(f"❌ Failed to fetch release issues for `{app_name}`.")
        return
    if not issues:
        await respond(f"No open release issue found for `{app_name}`.")
        return
    user = context["user_id"]
    try:
        await github.close_release_issue(
            cfg.repo,
            issues[0]["number"],
            comment=f"Promoted to Production by <@{user}> via `/doof promote`.",
        )
    except Exception:
        log.exception("Failed to promote %s", app_name)
        await respond(f"❌ Failed to promote `{app_name}`.")
        return
    deploy = await _nudge_release_gate(app_name, cfg)
    _release_requesters.pop(app_name, None)
    await respond(f"✅ `{app_name}` {issues[0]['title']} approved. {deploy}")


# How long the promote reply waits for the release-gate check it asks for. The
# check is a nudge: the gate also checks itself every five minutes, so the
# reply does not depend on it.
_GATE_NUDGE_TIMEOUT_SECONDS = 30


def _production_job_url(app_name: str, cfg) -> str:
    # Matches the Production deploy job k8s_apps/pipeline.py's release-resource
    # workflow generates for the `ol-application-<app>` Pulumi project
    # (deploy-<project>-<stack>).
    return concourse.job_url(
        cfg.pipeline, f"deploy-ol-application-{app_name}-production"
    )


async def _nudge_release_gate(app_name: str, cfg) -> str:
    """Ask Concourse to check the release gate now; say what happens next.

    Closing the issue is the approval. The deploy starts when a check of the
    gate records the close, and the gate checks every five minutes on its
    own. Asking for a check now usually saves that wait, but a check that
    succeeds can still miss a close made a second earlier (mitxonline #4204 on
    2026-10-29), so the reply never says a deploy started. Whether one did is
    on the release issue (`ops:consumed`, or `ops:refused` if Production
    refused it) and in the channel when the deploy lands.
    """
    url = _production_job_url(app_name, cfg)
    try:
        await asyncio.wait_for(
            concourse.check_resource(cfg.pipeline, f"{app_name}-release-gate"),
            _GATE_NUDGE_TIMEOUT_SECONDS,
        )
    except Exception:
        log.exception("Release-gate check failed for %s", app_name)
        return (
            "Production starts at the release gate's next scheduled check "
            "(within 5m); the bot could not confirm that Concourse checked it "
            f"sooner: {url}"
        )
    return (
        "Concourse was asked to check the release gate; Production starts when "
        f"a check records the close (within 5m): {url}"
    )


def _known_libraries() -> str:
    return ", ".join(f"`{name}`" for name in sorted(_libraries)) or "(none configured)"


async def _monorepo_package_job(respond, library: str, cfg, package: str | None):
    """Resolve a monorepo package to its publish job, or explain and return None.

    The packages come from the pipeline's live job list rather than from the
    registry: the publish pipelines discover them by walking the source
    checkout at generation time, so anything written down separately goes
    stale as soon as a package is added to the monorepo.
    """
    try:
        jobs = await concourse.list_jobs(cfg.pipeline, cfg.team)
    except Exception:
        log.exception("Failed to list jobs for %s", cfg.pipeline)
        await respond(
            f"❌ Could not read `{cfg.pipeline}`'s packages from Concourse. Check logs."
        )
        return None

    packages = sorted(
        job.removeprefix(cfg.package_job_prefix)
        for job in jobs
        if job.startswith(cfg.package_job_prefix)
    )
    if not package:
        await respond(
            f"`{library}` is a monorepo — name the package to publish: "
            f"`/doof publish {library} <package>`.\n"
            f"Packages: {', '.join(f'`{p}`' for p in packages)}"
        )
        return None
    if package not in packages:
        await respond(
            f"Unknown package `{package}` in `{library}`.\n"
            f"Packages: {', '.join(f'`{p}`' for p in packages)}"
        )
        return None
    return f"{cfg.package_job_prefix}{package}"


async def _resolve_library(repos, respond, library: str):
    """Return the LibraryConfig for *library*, or explain why there isn't one."""
    if library in _libraries:
        return _libraries[library]

    if library in _unpublishable_libraries:
        # Doof could publish these; Concourse cannot. Saying why beats
        # "unknown", which reads as a typo.
        await respond(
            f"`{library}` is not published from Concourse: "
            f"{_unpublishable_libraries[library]}"
        )
        return None

    # Apps and libraries are separate registries and separate verbs; an app
    # name here is a wrong-verb mistake, not an unknown name.
    hint = (
        f"`{library}` is an app, not a library — use `/doof release {library}`."
        if library in repos
        else f"Unknown library `{library}`."
    )
    await respond(f"{hint} Known libraries: {_known_libraries()}")
    return None


async def _cmd_publish(repos, ack, respond, command, _context):
    await ack()
    library, _, package = command["text"].strip().partition(" ")
    package = package.strip() or None

    if not library:
        await respond(
            f"Usage: `{_syntax('publish')}`.\nKnown libraries: {_known_libraries()}"
        )
        return

    cfg = await _resolve_library(repos, respond, library)
    if cfg is None:
        return

    if cfg.package_job_prefix:
        job = await _monorepo_package_job(respond, library, cfg, package)
        if job is None:
            return
    else:
        if package:
            await respond(
                f"`{library}` publishes a single artifact — drop the "
                f"`{package}` argument."
            )
            return
        job = cfg.publish_job

    # A build triggered into a paused pipeline is created and then never
    # scheduled, so reporting its URL would look like success and sit at
    # "pending" forever. Say so instead of triggering.
    try:
        if await concourse.pipeline_is_paused(cfg.pipeline, cfg.team):
            await respond(
                f"⏸️ `{cfg.pipeline}` is paused, so a build would never run. "
                f"Unpause it (`fly -t pr-main unpause-pipeline -p "
                f"{cfg.pipeline}`) and try again."
            )
            return
    except Exception:
        # Not worth blocking a publish on: worst case the user sees a build
        # that never starts, which is where they were before this check.
        log.exception("Failed to read paused state of %s", cfg.pipeline)

    what = f"{library} {package}" if package else library
    try:
        build_url = await concourse.trigger_job(cfg.pipeline, job, cfg.team)
    except Exception:
        log.exception("Failed to trigger publish for %s", what)
        await respond(f"❌ Failed to trigger publish for `{what}`. Check logs.")
        return
    await respond(
        f"📦 Publish to {cfg.registry} triggered for `{what}`. Build: {build_url}"
    )


async def _cmd_abandon(repos, ack, respond, command, _context):
    await ack()
    app_name = command["text"].strip()
    cfg, error = _resolve_app(repos, app_name, "abandon")
    if cfg is None:
        await _refuse(respond, error)
        return
    async with _app_lock(app_name):
        await _abandon(respond, app_name, cfg)


async def _abandon(respond, app_name, cfg):
    # Read before mutating: a lookup that failed after the cancel would report
    # "nothing abandoned" with the requests already deleted.
    try:
        in_flight = await github.in_flight_release(cfg.repo)
    except Exception:
        log.exception("Failed to check release state for %s", app_name)
        await respond(
            f"❌ Could not check `{app_name}`'s release state. Nothing abandoned "
            "or cancelled."
        )
        return
    try:
        cancelled = await github.cancel_hotfix_requests(cfg.repo)
    except Exception:
        log.exception("Failed to cancel hotfix requests for %s", app_name)
        await respond(
            f"❌ Failed while cancelling `{app_name}`'s pending hotfix requests; "
            "some may already be gone. Nothing abandoned."
        )
        return
    lines = [f"Cancelled the pending hotfix of `{sha[:7]}`." for sha in cancelled]
    if not in_flight:
        # The abandon job deletes the branch and tag of whatever version the
        # release resource last reported. With nothing in flight and no new
        # commits, that is the release production is running, and its tag is
        # the only record of what it runs.
        lines.append(f"No release is in flight for `{app_name}`; nothing to abandon.")
        await respond("\n".join(lines))
        return
    try:
        build_url = await concourse.trigger_job(
            cfg.pipeline, f"abandon-{app_name}-release"
        )
    except Exception:
        log.exception("Failed to trigger abandon for %s", app_name)
        # Any cancelled-hotfix lines already collected are a real state
        # change that happened before this failure; dropping them would tell
        # the user nothing was abandoned when their hotfix requests are
        # already gone.
        lines.append(f"❌ Failed to trigger release abandon for `{app_name}`.")
        await respond("\n".join(lines))
        return
    _release_requesters.pop(app_name, None)
    # The job deletes whatever version the release resource's last `check`
    # recorded, which can diverge from `in_flight` (read fresh from GitHub
    # branches here) if a `check` ran without a cut in between. Report the
    # trigger, not a specific deletion, and let the build's own output say
    # what it removed.
    lines.insert(
        0,
        f"🗑️ Release abandon triggered for `{app_name}` (last seen in flight: "
        f"{_describe_in_flight(in_flight)}). Build: {build_url}",
    )
    await respond("\n".join(lines))


# "{app}:{version}" for every promotion a click is currently driving, so a
# second click -- a double-tap, or two people racing on the same message --
# is refused rather than both closing the same issue. Nothing awaits between
# the check and the add, so it is atomic on the event loop (same reasoning as
# _checkbox_watchers). Process-local on purpose: a crash must not leave a
# promotion claimed forever.
_promoting: set[str] = set()


async def _update_promote_message(client, body, status: str) -> None:
    """Swap the clicked message's button for *status*, in place.

    Called only on a terminal outcome (promoted, or no open release issue
    remains for this version), never mid-flight: the message ts is stored
    nowhere, so nothing can repair this
    message later, and a message stranded without its button by a crash would
    be a release nobody can promote from Slack. Best-effort -- a failed edit
    is cosmetic and must never abort a promotion that is otherwise fine.
    """
    channel = (body.get("channel") or {}).get("id")
    message = body.get("message") or {}
    if not (channel and message.get("ts")):
        return
    try:
        blocks = [
            block
            for block in message.get("blocks") or []
            if block.get("type") != "actions"
        ]
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": status}]}
        )
        await client.chat_update(
            channel=channel, ts=message["ts"], text=status, blocks=blocks
        )
    except Exception:
        log.exception("Failed to update the promote message in %s", channel)


async def _handle_promote_button(repos, ack, body, say, client):
    await ack()
    # The button's value is "{app}:{version}", so it doubles as the claim key.
    value = body["actions"][0]["value"]
    app_name, version = value.split(":", 1)
    if value in _promoting:
        await say(f"⏳ `{app_name}` `{version}` is already being promoted.")
        return
    _promoting.add(value)
    try:
        await _promote_from_button(repos, body, say, client)
    finally:
        _promoting.discard(value)


async def _promote_from_button(repos, body, say, client):
    """Close the release issue behind the button, which approves the deploy.

    The clicked message is edited only on a terminal outcome (promoted, or no
    open release issue remains for this version). On a retryable failure it is
    left untouched -- button intact -- and the error is posted to the channel
    instead.
    """
    app_name, version = body["actions"][0]["value"].split(":", 1)
    cfg, error = _resolve_app(repos, app_name)
    if cfg is None:
        await say(f"⚠️ {error}")
        return
    user_id = body["user"]["id"]
    try:
        issues = await github.open_release_issues(cfg.repo)
    except Exception:
        log.exception("Failed to fetch issues for promote button: %s", app_name)
        await say(f"⚠️ Failed to fetch release issues for `{app_name}`.")
        return
    # The issue title is always just "Release {app_name}" -- the version only
    # ever appears in the body's "## Release <version>" header.
    matching = [i for i in issues if github.extract_version(i["body"]) == version]
    if not matching:
        # The issue is already closed: promoted some other way, abandoned
        # (the abandon job closes the gate issue labelled `ops:abandoned`),
        # closed by hand, or closed by an earlier click the bot died before
        # narrating. Settle the stale button rather than leave it reporting a
        # lookup failure, and keep the channel post neutral -- ✅ would read
        # as success for a release that never shipped.
        await _update_promote_message(
            client,
            body,
            # The info emoji is deliberate, not a confusable latin i.
            "ℹ️ No open release issue remains for this version — nothing to promote.",  # noqa: RUF001
        )
        await say(
            f"ℹ️ `{app_name}` `{version}` has no open release issue: "  # noqa: RUF001
            "already promoted or closed."
        )
        return
    try:
        await github.close_release_issue(
            cfg.repo,
            matching[0]["number"],
            comment=f"Promoted to Production by <@{user_id}> via Slack button.",
        )
    except Exception:
        log.exception("Failed to close issue for %s %s", app_name, version)
        await say(f"⚠️ Failed to promote `{app_name}` `{version}`.")
        return
    deploy = await _nudge_release_gate(app_name, cfg)
    _release_requesters.pop(app_name, None)
    await _update_promote_message(
        client,
        body,
        f"🚀 <@{user_id}> approved this for Production. {deploy}",
    )
    # The edit above fires no notification; this channel post is what does.
    await say(
        f"🚀 <@{user_id}> approved `{app_name}` `{version}` for Production. {deploy}"
    )


_CHECKBOX_POLL_SECONDS = 60

# Apps with a checkbox watcher already running, so a second
# `/doof wait-for-checkboxes` for the same app doesn't start a duplicate loop
# posting every progress message twice.
_checkbox_watchers: set[str] = set()


async def _watch_checkboxes(app, app_name, cfg, channel) -> None:
    """Poll a release checklist, reporting each author as they finish.

    Ported from the release-script bot's `wait for checkboxes`: the useful
    part is naming *who* is still outstanding, so the release manager can
    chase them, rather than reporting a bare unchecked count.

    Stops once everything is checked. The final "ready to promote" message and
    its button are left to `_notify_ready_to_promote`, which already covers
    every open release and dedupes on a label -- posting them here as well
    would double up.
    """
    try:
        while True:
            try:
                issues = await github.open_release_issues(cfg.repo)
            except Exception:
                log.exception("Checkbox watch: failed to poll issues for %s", app_name)
                await asyncio.sleep(_CHECKBOX_POLL_SECONDS)
                continue
            if not issues:
                await app.client.chat_postMessage(
                    channel=await slack_channels.resolve(app.client, channel),
                    text=(
                        f"`{app_name}`: no open release issue any more — "
                        "no longer watching checkboxes."
                    ),
                )
                return

            issue = issues[0]
            outstanding = github.unchecked_authors(issue["body"])
            if not outstanding:
                # Stop silently. `_notify_ready_to_promote` polls every open
                # release issue and posts the ready message *with the promote
                # button*, deduped by label -- announcing it here as well would
                # put two ready notifications in the channel, one of them
                # without the button.
                log.info(
                    "Checkbox watch: %s is fully checked; leaving the "
                    "ready-to-promote notification to the poller",
                    app_name,
                )
                return

            await asyncio.sleep(_CHECKBOX_POLL_SECONDS)

            try:
                refreshed = await github.open_release_issues(cfg.repo)
            except Exception:
                log.exception("Checkbox watch: failed to refresh %s", app_name)
                await asyncio.sleep(_CHECKBOX_POLL_SECONDS)
                continue
            if not refreshed:
                await asyncio.sleep(_CHECKBOX_POLL_SECONDS)
                continue
            still_outstanding = github.unchecked_authors(refreshed[0]["body"])
            newly_done = outstanding - still_outstanding
            if newly_done:
                await app.client.chat_postMessage(
                    channel=await slack_channels.resolve(app.client, channel),
                    text=(
                        f"Thanks for checking off your boxes, "
                        f"{await slack_users.format_authors(app.client, newly_done)}!"
                    ),
                )
    finally:
        _checkbox_watchers.discard(app_name)


def _checkbox_watch_preflight(repos, app_name):
    """Return (app, channel, None) if a watcher can start, else (None, None, error).

    The app is handed back rather than re-read from the module global by the
    caller, so the `_slack_app is None` check below is what narrows the type
    for everything downstream.
    """
    cfg, error = _resolve_app(repos, app_name, "wait-for-checkboxes")
    if cfg is None:
        return None, None, error
    channel = cfg.channel or os.environ.get("RELEASE_ANNOUNCE_CHANNEL")
    if not channel:
        return (
            None,
            None,
            f"No Slack channel configured for `{app_name}`, so there is nowhere "
            "to post checklist progress.",
        )
    if _slack_app is None:
        return None, None, "Bot is still starting up; try again in a moment."
    if app_name in _checkbox_watchers:
        return None, None, f"Already watching `{app_name}`'s release checklist."
    # Reserve the slot here rather than after the GitHub lookup below. Nothing
    # awaits between this check and the add, so it is atomic on the event loop;
    # claiming it later would let two concurrent invocations both pass this
    # check, interleave at open_release_issues, and start duplicate watchers
    # that post every progress message twice.
    _checkbox_watchers.add(app_name)
    return _slack_app, channel, None


async def _cmd_wait_for_checkboxes(repos, ack, respond, command, _context):
    """Watch a release checklist and report who is still outstanding."""
    await ack()
    app_name = command["text"].strip()
    app, channel, error = _checkbox_watch_preflight(repos, app_name)
    if error or app is None:
        await _refuse(respond, error)
        return
    cfg = repos[app_name]

    try:
        issues = await github.open_release_issues(cfg.repo)
    except Exception:
        log.exception("Failed to fetch release issues for %s", app_name)
        _checkbox_watchers.discard(app_name)
        await respond(f"❌ Failed to fetch release issues for `{app_name}`.")
        return
    if not issues:
        _checkbox_watchers.discard(app_name)
        await respond(f"No open release issue found for `{app_name}`.")
        return

    issue = issues[0]
    outstanding = github.unchecked_authors(issue["body"])
    checked, total = github.checklist_status(issue["body"])
    if not outstanding:
        _checkbox_watchers.discard(app_name)
        await respond(
            f"✅ `{app_name}`: all {total} checkboxes are already checked off. "
            f"<{issue['url']}|Release issue> is ready to promote."
        )
        return

    asyncio.create_task(  # noqa: RUF006
        _watch_checkboxes(app, app_name, cfg, channel)
    )
    await respond(
        f"👀 Watching `{app_name}`'s <{issue['url']}|release checklist> "
        f"({checked}/{total} checked).\n"
        f"Still waiting on: "
        f"{await slack_users.format_authors(app.client, outstanding)}"
    )


_COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


async def _request_hotfix(cfg, app_name, ref):
    """Request a hotfix of *ref*; return (full sha, None) or (None, error).

    The release resource refuses an in-flight release too, but only once the
    build is running, so it is checked here first. A request already pending
    for the same commit is reused, which is what makes `/doof hotfix` safe to
    re-run after a failed trigger.
    """
    try:
        sha = await github.resolve_commit(cfg.repo, ref)
    except Exception:
        log.exception("Failed to resolve %s in %s", ref, cfg.repo)
        return None, f"❌ Could not find commit `{ref}` in `{cfg.repo}`."
    try:
        in_flight = await github.in_flight_release(cfg.repo)
        pending = await github.pending_hotfix_requests(cfg.repo)
    except Exception:
        log.exception("Failed to check release state for %s", app_name)
        return None, (
            f"❌ Could not check `{app_name}`'s release state. Not requesting a hotfix."
        )
    if in_flight:
        return None, (
            f"Release {_describe_in_flight(in_flight)} is in flight for "
            f"`{app_name}`. Promote it or `/doof abandon {app_name}` before "
            "cutting a hotfix."
        )
    others = [p for p in pending if p != sha]
    if others:
        return None, (
            f"A hotfix of `{others[0][:7]}` is already pending for `{app_name}`. "
            f"`/doof abandon {app_name}` cancels it."
        )
    if sha not in pending:
        try:
            await github.request_hotfix(cfg.repo, sha)
        except Exception:
            log.exception("Failed to request hotfix of %s for %s", sha, app_name)
            return None, f"❌ Failed to request a hotfix of `{sha[:7]}`."
    return sha, None


async def _cmd_hotfix(repos, ack, respond, command, context):
    """Cut a hotfix: what production runs plus one commit.

    The SHA reaches the release job through a `hotfix/<sha>` tag, because
    triggering a Concourse job carries no parameters. The release resource's
    check sees the tag and hands the job a hotfix version; cutting it deletes
    the tag, whether or not the cut succeeds.
    """
    await ack()
    app_name, _, ref = command["text"].strip().partition(" ")
    ref = ref.strip().lower()
    cfg, error = _resolve_app(repos, app_name, "hotfix")
    if cfg is None:
        await _refuse(respond, error)
        return
    if not _COMMIT_SHA_RE.match(ref):
        problem = (
            f"`{_slack_escape(ref)}` is not a commit SHA."
            if ref
            else "`/doof hotfix` needs the commit to hotfix."
        )
        await _refuse(respond, f"{problem}\n{_usage_hint(repos, 'hotfix')}")
        return
    async with _app_lock(app_name):
        await _hotfix(respond, app_name, cfg, ref, context)


async def _hotfix(respond, app_name, cfg, ref, context):
    sha, error = await _request_hotfix(cfg, app_name, ref)
    if sha is None:
        await respond(error)
        return

    retry = (
        f"The hotfix is requested but not started: `/doof hotfix {app_name} "
        f"{sha[:7]}` retries, `/doof abandon {app_name}` cancels."
    )
    try:
        # Same reason as /doof release: without a check, the job binds the
        # version Concourse last recorded, which knows nothing of the request.
        await concourse.check_resource(cfg.pipeline, f"{app_name}-release")
        build_url = await concourse.trigger_job(
            cfg.pipeline, f"build-{app_name}-release-image"
        )
    except Exception:
        log.exception("Failed to trigger hotfix for %s", app_name)
        await respond(f"❌ Failed to trigger the hotfix build. {retry}")
        return

    requester = (context or {}).get("user_id")
    if requester:
        _release_requesters[app_name] = requester
    await respond(
        f"🚑 Hotfix of `{sha[:7]}` triggered for `{app_name}`: what production "
        f"runs plus that commit. Build: {build_url}"
    )


async def _cmd_help(
    repos: dict[str, config.AppConfig],
    ack: _SlackCallable,
    respond: _SlackCallable,
    _command: dict[str, Any],
    _context: dict[str, Any],
) -> None:
    await ack()
    # Name the apps too: the usage alone leaves <app> to guesswork, and a
    # guess at a legacy app is refused.
    await respond(f"{_USAGE}\n{_app_lists(repos)}")


def _format_elapsed(elapsed: timedelta) -> str:
    """Render a duration as its two largest units, e.g. `3d 4h` or `5m 2s`."""
    seconds = max(int(elapsed.total_seconds()), 0)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = [
        f"{value}{unit}"
        for value, unit in ((days, "d"), (hours, "h"), (minutes, "m"), (seconds, "s"))
        if value
    ]
    return " ".join(parts[:2]) or "0s"


# A poller is reported as overdue once its last iteration is this many poll
# intervals old: one late cycle is a slow GitHub, three is a wedged loop.
_POLL_OVERDUE_INTERVALS = 3


async def _cmd_uptime(
    _repos: dict[str, config.AppConfig],
    ack: _SlackCallable,
    respond: _SlackCallable,
    _command: dict[str, Any],
    _context: dict[str, Any],
) -> None:
    """Report that the bot is up, since when, and whether its pollers run.

    Doof's `uptime` mattered because Doof fell over; this bot's failure is
    quieter. A dropped Socket Mode connection makes slash commands time out
    with no error anywhere -- an answer here at least proves the connection
    is up -- and a wedged background loop stops announcements without a
    trace, which the per-poller heartbeat surfaces.
    """
    await ack()
    now = datetime.now(tz=UTC)
    pod = os.environ.get("HOSTNAME", "unknown pod")
    lines = [
        f"🟢 Up {_format_elapsed(now - _started_at)} "
        f"(since {_started_at:%Y-%m-%d %H:%M} UTC) on `{pod}`."
    ]
    for name, interval in _POLLERS.items():
        heartbeat = _poll_heartbeats.get(name)
        every = timedelta(seconds=interval)
        if heartbeat is None:
            # A first pass wedged inside an await never beats at all, so
            # measure from process start rather than reading as a fresh boot.
            status = f"• {name} poller: has not finished a pass yet"
            if now - _started_at > every * _POLL_OVERDUE_INTERVALS:
                status += f" ⚠️ overdue (runs every {_format_elapsed(every)})"
            lines.append(status)
            continue
        finished_at, ok = heartbeat
        age = now - finished_at
        status = f"• {name} poller: last pass {_format_elapsed(age)} ago"
        if age > every * _POLL_OVERDUE_INTERVALS:
            status += f" ⚠️ overdue (runs every {_format_elapsed(every)})"
        if not ok:
            status += " — some of its calls failed; see the bot's logs"
        lines.append(status)
    await respond("\n".join(lines))


async def _deployment_line(cfg: config.AppConfig, environment: str) -> str:
    """Describe what *cfg*'s app runs in *environment*, and any newer attempt.

    A Deployment is marked successful only when its whole QA or Production
    job finishes, after the Pulumi deploy. A job that deployed and then failed
    a later step leaves the environment running something newer than the last
    success, so the newest attempt is named whenever it is not that success.
    """
    try:
        report = await github.deployment_report(cfg.repo, environment)
    except Exception:
        log.exception("Failed to read %s deployments for %s", environment, cfg.repo)
        return f"• {environment}: ⚠️ could not read its deployments"
    deployment, newer = report["success"], report["newer"]
    line = f"• {environment}: " + (
        _describe_deployment(cfg, deployment)
        if deployment
        else "no successful deployment recorded"
    )
    if newer:
        age = _format_elapsed(datetime.now(tz=UTC) - newer["created_at"])
        sha = newer["sha"]
        commit = f"<https://github.com/{cfg.repo}/commit/{sha}|{sha[:7]}>"
        line += (
            f"\n    ↳ newer: {newer['version']} · {commit} started {age} ago, "
            f"not successful ({newer['state']}), so it may be partly deployed"
        )
    return line


def _describe_deployment(cfg: config.AppConfig, deployment: dict[str, Any]) -> str:
    version = deployment["version"]
    if deployment["url"]:
        # The github-deployments resource points this at the Concourse build.
        version = f"<{deployment['url']}|{version}>"
    sha = deployment["sha"]
    commit = f"<https://github.com/{cfg.repo}/commit/{sha}|{sha[:7]}>"
    age = _format_elapsed(datetime.now(tz=UTC) - deployment["deployed_at"])
    return f"{version} · {commit} · deployed {age} ago"


async def _cmd_version(
    repos: dict[str, config.AppConfig],
    ack: _SlackCallable,
    respond: _SlackCallable,
    command: dict[str, Any],
    _context: dict[str, Any],
) -> None:
    """Report the release version and commit RC and Production are running.

    Replaces Doof's `version` (what production runs) and `hash <rc|prod>`
    (the commit a server runs) with one answer read from the GitHub
    Deployments the pipeline records. Doof's `hash ci` has no equivalent: CI
    records no Deployment, and it redeploys every commit on the default
    branch, so the answer is the branch head.
    """
    await ack()
    app_filter = command["text"].strip()
    if app_filter:
        cfg, error = _resolve_app(repos, app_filter, "version")
        if cfg is None:
            await _refuse(respond, error)
            return
        targets = {app_filter: cfg}
    else:
        targets = config.release_workflow_apps(repos)
    if not targets:
        await respond(
            "No apps are on the release-resource workflow yet, so none has "
            "recorded deployments to report."
        )
        return
    environments = (github.PRODUCTION_ENVIRONMENT, github.RC_ENVIRONMENT)
    found = await asyncio.gather(
        *(
            _deployment_line(cfg, environment)
            for cfg in targets.values()
            for environment in environments
        )
    )
    lines = []
    for index, name in enumerate(targets):
        lines.append(f"*{name}*")
        lines += found[index * len(environments) : (index + 1) * len(environments)]
    legacy = [name for name, cfg in repos.items() if not cfg.release_workflow]
    if legacy and not app_filter:
        # Same reason as release-status: a silently short list reads as
        # "these are all the apps".
        lines.append(
            f"_Not shown -- the legacy pipeline records no deployments: "
            f"{', '.join(legacy)}._"
        )
    await respond("\n".join(lines))


_SUBCOMMANDS = {
    "release": _cmd_release,
    "hotfix": _cmd_hotfix,
    "preview": _cmd_preview,
    "release-notes": _cmd_release_notes,
    "release-status": _cmd_release_status,
    "wait-for-checkboxes": _cmd_wait_for_checkboxes,
    "promote": _cmd_promote,
    "publish": _cmd_publish,
    "abandon": _cmd_abandon,
    "version": _cmd_version,
    "uptime": _cmd_uptime,
    "help": _cmd_help,
}
_PRIVATE_SUBCOMMANDS = frozenset({"help", "uptime"})


def _in_channel(respond):
    """Wrap `respond` so its message posts visibly to the channel.

    Slash command responses are ephemeral (visible only to the invoker) by
    default; release state needs to be visible to the whole team.
    """

    async def wrapped(*args, **kwargs):
        kwargs.setdefault("response_type", "in_channel")
        await respond(*args, **kwargs)

    return wrapped


# How much of a command the announcement echoes. It is posted before a
# handler can decide to refuse, so anything anyone types reaches the channel;
# an attribution is one line, not a transcript.
_ECHO_LIMIT = 120


def _attribution(command: dict[str, Any], context: dict[str, Any] | None) -> str | None:
    """Name whoever ran a command, echoing what they typed, or None if unknown.

    The channel sees release state but not who asked for it, which is why
    `/doof promote` already records its requester by hand. Rendering it here,
    off the dispatcher, covers every subcommand including ones added later.
    """
    user = (context or {}).get("user_id") or command.get("user_id")
    if not user:
        return None
    invocation = f"{command.get('command', '/doof')} {command['text']}".strip()
    if len(invocation) > _ECHO_LIMIT:
        invocation = invocation[:_ECHO_LIMIT].rstrip() + "…"
    # Escape after cutting, or the cut lands mid-entity. The echo is whatever
    # someone typed and it goes inside a code span, so a backtick in it would
    # close the span and let the rest render as mrkdwn.
    invocation = _slack_escape(invocation).replace("`", "'")
    return f"<@{user}> requested `{invocation}`"


def _announcing(
    ack: _SlackCallable, respond: _SlackCallable, attribution: str
) -> _SlackCallable:
    """Wrap `ack` so acknowledging a command announces who ran it, once.

    Hooking `ack` rather than announcing from the dispatcher keeps the extra
    round-trip off the path to Slack's 3s acknowledgement deadline, while
    still putting the announcement ahead of the GitHub and Concourse work a
    handler does before it says anything itself.
    """
    announced = False

    async def wrapped(*args, **kwargs):
        nonlocal announced
        await ack(*args, **kwargs)
        if not announced:
            announced = True
            try:
                await respond(attribution, response_type="in_channel")
            except Exception:
                # Hooking `ack` puts this ahead of everything the handler
                # does, so a failure to say who ran a command must not take
                # the command itself down with it.
                log.exception("Failed to announce who ran a command")

    return wrapped


async def _cmd_doof(repos, ack, respond, command, context):
    parts = command["text"].split(None, 1)
    if not parts or parts[0] not in _SUBCOMMANDS:
        await ack()
        await respond(_USAGE)
        return
    subcommand, *rest = parts
    sub_command = dict(command, text=rest[0] if rest else "")
    # Release state is for the whole channel; asking how to use the bot, or
    # whether it is alive, is not.
    if subcommand not in _PRIVATE_SUBCOMMANDS:
        respond = _in_channel(respond)
        # Built from `command`, not `sub_command`: the echo is what was typed.
        attribution = _attribution(command, context)
        if attribution:
            ack = _announcing(ack, respond, attribution)
    await _SUBCOMMANDS[subcommand](repos, ack, respond, sub_command, context)


def create_app():
    global _slack_app  # noqa: PLW0603
    repos = config.load_repos_config()
    _libraries.update(config.load_libraries_config())
    _unpublishable_libraries.update(config.load_unpublishable_libraries())
    # Before any handler can run: an author resolved once is cached for a day,
    # so installing the overrides late would leave the first poll's fallbacks
    # in place well past boot.
    slack_users.configure_overrides(config.load_slack_user_overrides())
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    app.command("/doof")(partial(_cmd_doof, repos))
    app.action("promote_production")(partial(_handle_promote_button, repos))
    _slack_app = app
    return app, repos


_READY_TO_PROMOTE_POLL_SECONDS = 120


def _announce_channel(cfg) -> str | None:
    """Return the Slack channel for proactive posts about *cfg*'s app, if any."""
    return cfg.channel or os.environ.get("RELEASE_ANNOUNCE_CHANNEL")


def _ready_to_promote_blocks(
    app_name: str, version: str, issue_url: str, requester: str | None = None
) -> list[dict[str, object]]:
    cc = f" cc <@{requester}>" if requester else ""
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"✅ *`{app_name}` {version}* — all checklist items are "
                    f"checked off. <{issue_url}|Release issue> ready for "
                    f"promotion.{cc}"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🚀 Promote to Production"},
                    "style": "primary",
                    "action_id": "promote_production",
                    "value": f"{app_name}:{version}",
                    # One stray click otherwise ships to Production. Doof's
                    # "Finish the release" button asked first, too.
                    "confirm": {
                        "title": {
                            "type": "plain_text",
                            "text": "Promote to Production?",
                        },
                        "text": {
                            "type": "plain_text",
                            "text": (
                                "This closes the release issue and deploys "
                                f"{app_name} {version} to Production."
                            ),
                        },
                        "confirm": {"type": "plain_text", "text": "Promote"},
                        "deny": {"type": "plain_text", "text": "Cancel"},
                    },
                }
            ],
        },
    ]


async def _notify_ready_to_promote(app, repos) -> bool:
    """Notify Slack for any open release issue whose checklist is now fully checked.

    Marks each notified issue with `github.PROMOTE_READY_LABEL` so repeated
    polls don't re-send the same notification every cycle.

    Returns whether every call in the pass succeeded, for the `/doof uptime`
    heartbeat. Each failure is caught per app so one broken app cannot starve
    the rest -- which also means a pass in which *every* call fails (a revoked
    GitHub App key, GitHub down) raises nothing, and only this says so. The
    two release-progress steps return the same.
    """
    ok = True
    for app_name, cfg in repos.items():
        channel = _announce_channel(cfg)
        if not channel:
            continue
        try:
            issues = await github.open_release_issues(cfg.repo)
        except Exception:
            ok = False
            log.exception("Failed to poll release issues for %s", app_name)
            continue
        for issue in issues:
            if set(issue["labels"]).intersection(github.PROMOTE_READY_LABELS):
                continue
            if not github.is_fully_checked(issue["body"]):
                continue
            version = github.extract_version(issue["body"])
            if not version:
                # The promote button's value is "{app_name}:{version}", and
                # _handle_promote_button matches it back to an issue via
                # extract_version(issue["body"]). Falling back to the title
                # here (which never contains a version -- it's always just
                # "Release {app_name}") would produce a button that can never
                # find its issue. Skip rather than send a broken button.
                log.warning(
                    "Release issue #%s for %s has no version header; skipping"
                    " ready-to-promote notification",
                    issue["number"],
                    app_name,
                )
                continue
            try:
                await app.client.chat_postMessage(
                    channel=await slack_channels.resolve(app.client, channel),
                    text=f"✅ `{app_name}` {version} is ready to promote.",
                    blocks=_ready_to_promote_blocks(
                        app_name,
                        version,
                        issue["url"],
                        _release_requesters.get(app_name),
                    ),
                )
            except Exception:
                ok = False
                log.exception(
                    "Failed to send ready-to-promote notification for %s", app_name
                )
                continue
            # The mention has been delivered. Dropping it here keeps a failed
            # add_issue_label below -- which leaves the issue eligible on
            # every 120s poll -- from re-pinging the same person on each
            # repeat; the duplicate message is a nuisance, a repeating
            # notification is not.
            _release_requesters.pop(app_name, None)
            try:
                await github.add_issue_label(
                    cfg.repo, issue["number"], github.PROMOTE_READY_LABEL
                )
            except Exception:
                ok = False
                # The notification already went out -- only the "don't repeat
                # this" bookkeeping failed. Log distinctly from a notify
                # failure so the two aren't conflated; worst case this issue
                # gets re-notified next poll cycle, which is a minor nuisance
                # next to silently never notifying at all.
                log.exception(
                    "Sent ready-to-promote notification for %s but failed to"
                    " apply %s label; issue #%s may be re-notified next poll",
                    app_name,
                    github.PROMOTE_READY_LABEL,
                    issue["number"],
                )
    return ok


async def _poll_ready_to_promote_loop(app, repos) -> None:
    # Check-then-sleep, not sleep-then-check: a release can already be fully
    # checked and waiting at the moment the bot starts (e.g. after a
    # deploy/restart), and sleeping first would delay that notification by up
    # to _READY_TO_PROMOTE_POLL_SECONDS for no reason.
    while True:
        ok = False
        try:
            ok = await _notify_ready_to_promote(app, repos)
        except Exception:
            # Without this, any unexpected exception here kills the
            # background task permanently and silently -- the feature stops
            # working with no log until someone notices and restarts the pod.
            log.exception("ready-to-promote poll iteration failed")
        _poll_heartbeats[_READY_TO_PROMOTE_POLLER] = (datetime.now(tz=UTC), ok)
        await asyncio.sleep(_READY_TO_PROMOTE_POLL_SECONDS)


_RELEASE_PROGRESS_POLL_SECONDS = 120
# Doof nagged when a deploy had not landed within a timeout and then repeated
# every 24h; that nag was the only thing in Slack that made a stalled release
# visible. ol-analytics-api's releases/2026.8.3.1 sat cut-but-unfinished from
# 2026-08-03 with no red build and no message anywhere.
# 16h rather than 24h because reminders only go out in the 10:00 ET window
# below: a release cut by 18:00 ET is reminded about the next morning instead
# of the morning after.
_DEFAULT_STUCK_AFTER_HOURS = 16.0

# The reminder goes out in one fixed daily window, not 24h after the last one.
# "The last one" lived only in process memory, so every bot rollout or pod
# reschedule reset it and the next poll re-posted: xpro 2026.10.27.1 was
# reminded at 13:35, 15:38 and 23:07 ET on 2026-10-28, each a new pod. A fixed
# window needs no stored state. A restart repeats a reminder only if it lands
# inside the window, and the reminder arrives in working hours rather than
# whenever the release happened to be cut. The window spans many polls, so a
# failed post is retried.
_STUCK_NAG_TIMEZONE = ZoneInfo("America/New_York")
_STUCK_NAG_AT = time(10, 0)
_STUCK_NAG_WINDOW = timedelta(minutes=30)


def _now() -> datetime:
    return datetime.now(tz=UTC)


def _stuck_nag_window_start(now: datetime) -> datetime | None:
    """Return when today's reminder window opened if *now* is inside it, else None."""
    local = now.astimezone(_STUCK_NAG_TIMEZONE)
    start = datetime.combine(local.date(), _STUCK_NAG_AT, tzinfo=_STUCK_NAG_TIMEZONE)
    if start <= local < start + _STUCK_NAG_WINDOW:
        return start
    return None


def _stuck_release_after() -> timedelta:
    raw = os.environ.get("RELEASE_STUCK_AFTER_HOURS", "").strip()
    if not raw:
        return timedelta(hours=_DEFAULT_STUCK_AFTER_HOURS)
    try:
        return timedelta(hours=float(raw))
    except ValueError:
        log.warning(
            "RELEASE_STUCK_AFTER_HOURS=%r is not a number; using %sh",
            raw,
            _DEFAULT_STUCK_AFTER_HOURS,
        )
        return timedelta(hours=_DEFAULT_STUCK_AFTER_HOURS)


@dataclass
class ReleaseProgressState:
    """What the release-progress poller has already said, held in process.

    `last_version` is seeded on first observation rather than starting
    empty, because the poller announces *transitions*: an empty map would
    replay every app's current RC and Production deployment into Slack on
    every bot restart. The cost of seeding is at most one missed announcement,
    when the bot restarts in the window between a deploy landing and the next
    poll -- much cheaper than a release channel that repeats itself, which is
    the failure that makes people mute it.

    `nagged_at` only stops repeat posts within one daily reminder window.
    The window itself is what limits a stuck release to one reminder a day
    across restarts (see `_stuck_nag_window_start`).
    """

    # The release version of the newest successful deployment, not its
    # GitHub Deployment id: the QA job records a fresh Deployment every time
    # an infra-only merge re-runs it, so the id changes with no new release.
    # None means "polled and confirmed no successful deployment yet" -- a real
    # baseline, distinct from a (app, environment) pair never having been
    # polled at all (absent from the dict). See _announce_deployments.
    last_version: dict[tuple[str, str], str | None] = field(default_factory=dict)
    nagged_at: dict[tuple[str, str], datetime] = field(default_factory=dict)
    # Refused issues already reported, by (app, issue number). An app absent
    # from `refusals_seeded` has not been polled yet; its first poll records
    # the refusals from before this process started without posting them.
    refusals_announced: set[tuple[str, int]] = field(default_factory=set)
    refusals_seeded: set[str] = field(default_factory=set)


def _format_age(age: timedelta) -> str:
    hours = int(age.total_seconds() // 3600)
    return f"{hours}h" if hours < 24 else f"{hours // 24}d"  # noqa: PLR2004


async def _outstanding_mentions(client, issue) -> str:
    """Render the authors with unchecked items on *issue*, or "" for none.

    The RC announcement is how an engineer learns their commit is on RC and
    needs verifying, so it names them rather than leaving every author to
    notice a channel message. An author with several items is named once, and
    one whose items are all checked is not named. A lookup failure drops the
    names, not the announcement.
    """
    if not issue or not issue.get("body"):
        return ""
    outstanding = github.unchecked_authors(issue["body"])
    if not outstanding:
        return ""
    try:
        return await slack_users.format_authors(client, outstanding)
    except Exception:
        log.exception("Failed to render the outstanding authors of %s", issue["url"])
        return ""


async def _milestone_text(app_name, cfg, environment, deployment, client) -> str:
    """Render the Slack message for a deployment that just reached success."""
    version = deployment["version"]
    issue = None
    try:
        issue = await github.release_issue_for_version(cfg.repo, version)
    except Exception:
        # The milestone still goes out without the link; "what is in this
        # release" is one click worse, not missing.
        log.exception("Failed to find the release issue for %s %s", app_name, version)
    if issue is None and environment == github.RC_ENVIRONMENT:
        # Nobody is chased for a release whose issue was not found, and the
        # version is recorded as announced, so leave a trace.
        log.warning(
            "No release issue found for %s %s; its authors were not mentioned",
            app_name,
            version,
        )
    link = f" <{issue['url']}|Release issue>" if issue else ""
    if environment == github.PRODUCTION_ENVIRONMENT:
        return f"🎉 `{app_name}` {version} is live in production.{link}"
    text = (
        f"🚢 `{app_name}` {version} is deployed to RC.{link}"
        " Check off your items once you have verified them."
    )
    if mentions := await _outstanding_mentions(client, issue):
        text += f"\nStill waiting on: {mentions}"
    return text


async def _announce_deployments(app, repos, state: ReleaseProgressState) -> bool:
    """Post to Slack when an app's RC or Production deployment changes.

    Reads GitHub Deployments rather than watching the cluster: the pipeline
    already records one per environment (`_define_release_resources`), and
    they carry the release version and therefore the release issue, which a
    Kubernetes rollout event does not.

    This does not replace kubewatch-webhook-handler, and does not duplicate it
    either. kubewatch reports *rollouts* -- every workload, including infra
    changes that are not releases -- routed by the `ol.mit.edu/slack-channel`
    pod label, which most workloads do not set, so it lands in the ops
    channel. This reports *releases*, in the product channel from
    bridge.settings.apps, naming the version and linking what is in it. The
    two only collide if a team labels its workload with the same product
    channel; that is a per-workload choice, not something to arbitrate here.
    """
    ok = True
    for app_name, cfg in repos.items():
        channel = _announce_channel(cfg)
        if not channel:
            continue
        for environment in (github.RC_ENVIRONMENT, github.PRODUCTION_ENVIRONMENT):
            try:
                deployment = await github.latest_successful_deployment(
                    cfg.repo, environment
                )
            except Exception:
                ok = False
                log.exception(
                    "Failed to read %s deployments for %s", environment, app_name
                )
                continue
            key = (app_name, environment)
            # Distinguish "never polled this (app, environment)" from "polled
            # it and there was no successful deployment yet" -- both leave
            # `.get(key)` as None, but only the former should suppress the
            # announcement. Collapsing them missed a brand-new app's very
            # first deployment: it would poll None once (recording nothing),
            # then see a real id with no key present and treat that first
            # real deployment as the restart-seed case instead of a genuine
            # no-deployment -> deployed transition.
            first_observation = key not in state.last_version
            if deployment is None:
                state.last_version.setdefault(key, None)
                continue
            previous = state.last_version.get(key)
            state.last_version[key] = deployment["version"]
            if first_observation or previous == deployment["version"]:
                # First observation seeds the watcher; an unchanged version is
                # the steady state between releases, including every
                # infra-only QA re-run, which redeploys the same release under
                # a new Deployment id. A hotfix is announced: it is cut as a
                # new version.
                continue
            try:
                await app.client.chat_postMessage(
                    channel=await slack_channels.resolve(app.client, channel),
                    text=await _milestone_text(
                        app_name, cfg, environment, deployment, app.client
                    ),
                )
            except Exception:
                ok = False
                log.exception(
                    "Failed to announce the %s deployment of %s", environment, app_name
                )
                # Put the previous version back so the next poll retries this
                # milestone instead of silently treating it as announced.
                state.last_version[key] = previous
    return ok


async def _find_release_issue(app_name, cfg, version) -> dict[str, Any] | None:
    """Return the release issue for *version*, or None if it is missing or errors."""
    try:
        return await github.release_issue_for_version(cfg.repo, version)
    except Exception:
        log.exception("Failed to find the release issue for %s %s", app_name, version)
        return None


async def _stuck_release_text(app_name, cfg, in_flight, age: timedelta, client) -> str:
    version = in_flight["version"]
    reached = set()
    for environment in (github.RC_ENVIRONMENT, github.PRODUCTION_ENVIRONMENT):
        deployment = await github.latest_successful_deployment(cfg.repo, environment)
        if deployment is not None and deployment["version"] == version:
            reached.add(environment)
    if github.PRODUCTION_ENVIRONMENT in reached:
        # The deploy landed and `action: finish` did not. The release branch
        # outliving its production deploy is what freezes the calver counter,
        # so the next release silently reuses this version number.
        state_line = (
            "It reached production but the release never finished, so its "
            "branch is still open and the next release will collide with "
            "this version."
        )
    elif github.RC_ENVIRONMENT in reached:
        state_line = "It is deployed to RC and waiting to be promoted."
    else:
        state_line = "It has not reached RC."
    issue = await _find_release_issue(app_name, cfg, version)
    branch_link = f"<{in_flight['url']}|{in_flight['branch']}>"
    link = branch_link
    if issue:
        link = f"<{issue['url']}|Release issue>"
        # After production the leftover branch is the problem, so link it too.
        if github.PRODUCTION_ENVIRONMENT in reached:
            link += f" · {branch_link}"
    # Only RC is waiting on verification: before it there is nothing to
    # verify, and after production the checklist gates nothing.
    waiting = ""
    if reached == {github.RC_ENVIRONMENT} and (
        mentions := await _outstanding_mentions(client, issue)
    ):
        waiting = f"\nStill waiting on: {mentions}"
    return (
        f"⏳ `{app_name}` {version} was cut {_format_age(age)} ago and is not "
        f"finished. {state_line} {link}{waiting}\n"
        f"`/doof release {app_name} {_SUPERSEDE} {version}` replaces it with a "
        f"new release; `/doof abandon {app_name}` drops it."
    )


async def _nag_stuck_releases(app, repos, state: ReleaseProgressState) -> bool:
    """Report releases that were cut and never shipped, once a day."""
    now = _now()
    window_start = _stuck_nag_window_start(now)
    if window_start is None:
        return True
    ok = True
    threshold = _stuck_release_after()
    for app_name, cfg in repos.items():
        channel = _announce_channel(cfg)
        if not channel:
            continue
        try:
            in_flight = await github.in_flight_release(cfg.repo)
        except Exception:
            ok = False
            log.exception("Failed to check for a stuck release for %s", app_name)
            continue
        if not in_flight or in_flight.get("cut_at") is None:
            continue
        age = now - in_flight["cut_at"]
        if age < threshold:
            continue
        key = (app_name, in_flight["version"])
        last_nag = state.nagged_at.get(key)
        if last_nag is not None and last_nag >= window_start:
            continue
        try:
            text = await _stuck_release_text(app_name, cfg, in_flight, age, app.client)
        except Exception:
            ok = False
            log.exception(
                "Failed to describe the stuck release %s for %s",
                in_flight["version"],
                app_name,
            )
            continue
        try:
            await app.client.chat_postMessage(
                channel=await slack_channels.resolve(app.client, channel), text=text
            )
        except Exception:
            ok = False
            log.exception(
                "Failed to report the stuck release %s for %s",
                in_flight["version"],
                app_name,
            )
            continue
        state.nagged_at[key] = now
    return ok


# How far back to look for a refused release issue. A refusal is reported on
# the next poll; this bounds the query.
_REFUSAL_LOOKBACK = timedelta(days=1)
# On the first pass after the bot starts, a refusal labelled this long before
# the start is still reported. The common restart is a deploy of the bot, which
# takes minutes, and a refusal landing in it is exactly the one nobody saw.
# One reported just before a restart may be reported twice.
_REFUSAL_STARTUP_GRACE = timedelta(minutes=15)


def _is_gate_title(app_name: str, title: str) -> bool:
    """Return whether *title* is one of *app_name*'s release gate titles.

    mit-learn and mit-learn-nextjs share a repository, so a refused issue is
    attributed to an app by its whole title, as the pipeline's
    supersede_gate_issues.py does.
    """
    # The bare title names no version, and Production refuses it.
    shapes = (
        rf"Release {re.escape(app_name)} infrastructure @ \S+",
        rf"Release {re.escape(app_name)} \S+",
        rf"Release {re.escape(app_name)}",
    )
    return any(re.fullmatch(shape, title.strip()) for shape in shapes)


async def _announce_refusals(app, repos, state: ReleaseProgressState) -> bool:
    """Post to Slack when Production refuses a release issue it took.

    The Production job's assert-gate-names-deploy refuses an approval that no
    longer matches what would deploy -- usually because QA moved past the
    ol-infrastructure commit the issue approved -- and labels the issue
    `ops:refused`. Before the label, a refusal was only a red build, and the
    Slack thread said the deploy had started.

    The label is best-effort: Concourse can lose an `on_failure` hook. So its
    absence never means the deploy went ahead; that is only ever read from the
    GitHub Deployment (`_announce_deployments`). An issue also labelled
    abandoned is not reported: it was retired, so nobody is waiting on it.
    """
    ok = True
    since = _now() - _REFUSAL_LOOKBACK
    for app_name, cfg in repos.items():
        channel = _announce_channel(cfg)
        if not channel:
            continue
        try:
            refused = await github.refused_release_issues(cfg.repo, since)
        except Exception:
            ok = False
            log.exception("Failed to look for refused release issues for %s", app_name)
            continue
        seeding = app_name not in state.refusals_seeded
        for issue in refused:
            if not _is_gate_title(app_name, issue["title"]):
                continue
            key = (app_name, issue["number"])
            if key in state.refusals_announced:
                continue
            from_before_start = (
                issue["updated_at"] is None
                or issue["updated_at"] < _started_at - _REFUSAL_STARTUP_GRACE
            )
            if (seeding and from_before_start) or github.release_state(
                issue["labels"]
            ) != "refused":
                state.refusals_announced.add(key)
                continue
            version = github.extract_version(issue["body"]) or issue["title"]
            try:
                await app.client.chat_postMessage(
                    channel=await slack_channels.resolve(app.client, channel),
                    text=(
                        f"⛔ `{app_name}` {version} was not deployed: Production "
                        f"refused <{issue['url']}|#{issue['number']}> because it "
                        "no longer matches what would deploy, usually because QA "
                        "has moved past the ol-infrastructure commit it approved. "
                        "Approve the newest release issue instead. "
                        f"{_production_job_url(app_name, cfg)}"
                    ),
                )
            except Exception:
                ok = False
                log.exception(
                    "Failed to report the refusal of %s #%s", app_name, issue["number"]
                )
                continue
            state.refusals_announced.add(key)
        state.refusals_seeded.add(app_name)
    return ok


async def _poll_release_progress_loop(app, repos) -> None:
    state = ReleaseProgressState()
    while True:
        ok = True
        for step, name in (
            (_announce_deployments, "deployment milestone"),
            (_announce_refusals, "refused release"),
            (_nag_stuck_releases, "stuck release"),
        ):
            try:
                ok = await step(app, repos, state) and ok
            except Exception:
                # Neither step may take the other down, and an unexpected
                # exception here must not kill the task permanently and
                # silently the way an unguarded loop body would.
                log.exception("%s poll iteration failed", name)
                ok = False
        _poll_heartbeats[_RELEASE_PROGRESS_POLLER] = (datetime.now(tz=UTC), ok)
        await asyncio.sleep(_RELEASE_PROGRESS_POLL_SECONDS)


# poller name -> poll interval in seconds, for `/doof uptime`.
_POLLERS = {
    _READY_TO_PROMOTE_POLLER: _READY_TO_PROMOTE_POLL_SECONDS,
    _RELEASE_PROGRESS_POLLER: _RELEASE_PROGRESS_POLL_SECONDS,
}


async def _report_unreachable_channels(app, repos) -> None:
    """Log configured channels the bot cannot post to, at boot.

    Every proactive message is fire-and-forget inside a poll loop, so a
    channel the bot was never invited to shows up only as a failed post
    buried in the logs hours later. Say it once, at startup, naming the
    channels.
    """
    configured = {
        channel
        for cfg in repos.values()
        if (channel := _announce_channel(cfg)) is not None
    }
    if not configured:
        return
    try:
        missing = await slack_channels.unresolvable(app.client, configured)
    except Exception:
        log.exception("Failed to check configured Slack channels at startup")
        return
    if missing:
        log.error(
            "Cannot resolve %s of %s configured Slack channel(s) to an id: %s."
            " Posts to these will fail with channel_not_found until the bot is"
            " invited to them (private channels are only visible to members)",
            len(missing),
            len(configured),
            ", ".join(missing),
        )


async def _report_unusable_user_overrides(app) -> None:
    """Log author overrides pointing at an unmentionable Slack account, at boot.

    Same reasoning as _report_unreachable_channels: the override list exists so
    that certain authors get a real ping, and a stale or mistyped id in it fails
    by producing a mention that notifies nobody -- which, read in the channel,
    is indistinguishable from the bug the list was added to fix.
    """
    try:
        unusable = await slack_users.unusable_overrides(app.client)
    except Exception:
        log.exception("Failed to check the Slack user overrides at startup")
        return
    if unusable:
        log.error(
            "%s SLACK_USER_OVERRIDES entr(ies) point at an account that cannot "
            "be mentioned, so those authors will be named without being "
            "notified: %s",
            len(unusable),
            "; ".join(unusable),
        )


async def main():
    app, repos = create_app()
    # Channel reachability is checked across every registered app, including
    # the legacy ones: an unresolvable channel is the kind of thing you want
    # to have found before the app is migrated, not on its first release.
    await _report_unreachable_channels(app, repos)
    await _report_unusable_user_overrides(app)
    watched = config.release_workflow_apps(repos)
    log.info(
        "Polling %s of %s registered app(s) on the release-resource workflow: %s",
        len(watched),
        len(repos),
        ", ".join(watched) or "none",
    )
    asyncio.create_task(_poll_ready_to_promote_loop(app, watched))  # noqa: RUF006
    asyncio.create_task(_poll_release_progress_loop(app, watched))  # noqa: RUF006
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    await handler.start_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
