"""Slack Bolt async app — all command and action handlers for the release bot."""

import asyncio
import logging
import os
from functools import partial

import bot_config as config
import concourse_client as concourse
import github_client as github
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

log = logging.getLogger(__name__)


_USAGE = (
    "Usage:\n"
    "• `/doof release <app>` — cut a release\n"
    "• `/doof release-notes <app>` — show unreleased commits\n"
    "• `/doof release-status [app]` — check release issue status\n"
    "• `/doof promote <app>` — promote to production\n"
    "• `/doof publish <app>` — publish a library\n"
    "• `/doof hotfix <app> <commit>` — trigger a hotfix\n"
    "• `/doof abandon <app>` — abandon an in-progress release\n"
)


async def _cmd_release(repos, ack, respond, command, _context):
    await ack()
    app_name = command["text"].strip()
    if app_name not in repos:
        await respond(f"Unknown app `{app_name}`. Known apps: {', '.join(repos)}")
        return
    cfg = repos[app_name]
    try:
        build_url = await concourse.trigger_job(cfg.pipeline, "create-release")
    except Exception:
        log.exception("Failed to trigger release for %s", app_name)
        await respond(f"❌ Failed to trigger release for `{app_name}`. Check logs.")
        return
    await respond(f"🚀 Release triggered for `{app_name}`. Build: {build_url}")


async def _cmd_release_notes(repos, ack, respond, command, _context):
    await ack()
    app_name = command["text"].strip()
    if app_name not in repos:
        await respond(f"Unknown app `{app_name}`. Known apps: {', '.join(repos)}")
        return
    cfg = repos[app_name]
    try:
        commits = await github.commits_since_last_tag(cfg.repo)
    except Exception:
        log.exception("Failed to fetch release notes for %s", app_name)
        await respond(f"❌ Failed to fetch release notes for `{app_name}`.")
        return
    if not commits:
        await respond(
            f"*Release notes for `{app_name}`*\n_(no commits since last release)_"
        )
        return
    lines = [f"• `{c['sha'][:8]}` {c['message'].splitlines()[0]}" for c in commits]
    await respond(f"*Release notes for `{app_name}`*\n" + "\n".join(lines))


async def _cmd_release_status(repos, ack, respond, command, _context):
    await ack()
    app_filter = command["text"].strip() or None
    if app_filter and app_filter not in repos:
        await respond(f"Unknown app `{app_filter}`. Known apps: {', '.join(repos)}")
        return
    targets = {app_filter: repos[app_filter]} if app_filter else repos
    lines = []
    for name, cfg in targets.items():
        try:
            issues = await github.open_release_issues(cfg.repo)
        except Exception:
            log.exception("Failed to fetch release status for %s", name)
            lines.append(f"• *{name}*: ⚠️ error fetching status")
            continue
        status = f"🟡 {issues[0]['title']}" if issues else "✅ no open release"
        lines.append(f"• *{name}*: {status}")
    await respond("\n".join(lines))


async def _cmd_promote(repos, ack, respond, command, context):
    await ack()
    app_name = command["text"].strip()
    if app_name not in repos:
        await respond(f"Unknown app `{app_name}`. Known apps: {', '.join(repos)}")
        return
    cfg = repos[app_name]
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
    await respond(
        f"✅ `{app_name}` {issues[0]['title']} promoted. Production deploy triggered."
    )


async def _cmd_publish(repos, ack, respond, command, _context):
    await ack()
    library = command["text"].strip()
    if library not in repos:
        await respond(f"Unknown library `{library}`. Known apps: {', '.join(repos)}")
        return
    cfg = repos[library]
    try:
        build_url = await concourse.trigger_job(cfg.pipeline, "publish")
    except Exception:
        log.exception("Failed to trigger publish for %s", library)
        await respond(f"❌ Failed to trigger publish for `{library}`.")
        return
    await respond(f"📦 Publish triggered for `{library}`. Build: {build_url}")


async def _cmd_hotfix(repos, ack, respond, command, _context):
    await ack()
    parts = command["text"].split(None, 1)
    if len(parts) != 2:  # noqa: PLR2004
        await respond("Usage: `/doof hotfix <app> <commit-hash>`")
        return
    app_name, commit_hash = parts
    if app_name not in repos:
        await respond(f"Unknown app `{app_name}`. Known apps: {', '.join(repos)}")
        return
    cfg = repos[app_name]
    try:
        build_url = await concourse.trigger_job(cfg.pipeline, "create-hotfix")
    except Exception:
        log.exception("Failed to trigger hotfix for %s", app_name)
        await respond(f"❌ Failed to trigger hotfix for `{app_name}`.")
        return
    await respond(
        f"🔧 Hotfix job triggered for `{app_name}`. Build: {build_url}\n"
        f"Commit `{commit_hash}` must be set via the pipeline's hotfix resource "
        f"— the Concourse trigger API does not accept runtime variables."
    )


async def _cmd_abandon(repos, ack, respond, command, _context):
    await ack()
    app_name = command["text"].strip()
    if app_name not in repos:
        await respond(f"Unknown app `{app_name}`. Known apps: {', '.join(repos)}")
        return
    cfg = repos[app_name]
    try:
        build_url = await concourse.trigger_job(
            cfg.pipeline, f"abandon-{app_name}-release"
        )
    except Exception:
        log.exception("Failed to trigger abandon for %s", app_name)
        await respond(f"❌ Failed to trigger release abandon for `{app_name}`.")
        return
    await respond(f"🗑️ Release abandon triggered for `{app_name}`. Build: {build_url}")


async def _handle_promote_button(repos, ack, body, say):
    await ack()
    value = body["actions"][0]["value"]
    app_name, version = value.split(":", 1)
    if app_name not in repos:
        await say(f"⚠️ Unknown app `{app_name}`.")
        return
    cfg = repos[app_name]
    user_id = body["user"]["id"]
    try:
        issues = await github.open_release_issues(cfg.repo)
    except Exception:
        log.exception("Failed to fetch issues for promote button: %s", app_name)
        await say(f"⚠️ Failed to fetch release issues for `{app_name}`.")
        return
    matching = [i for i in issues if version in i["title"]]
    if not matching:
        await say(f"⚠️ Could not find open release issue for `{app_name}` `{version}`.")
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
    await say(
        f"🚀 <@{user_id}> promoted `{app_name}` `{version}` to Production. "
        "Concourse deploy triggered."
    )


_SUBCOMMANDS = {
    "release": _cmd_release,
    "release-notes": _cmd_release_notes,
    "release-status": _cmd_release_status,
    "promote": _cmd_promote,
    "publish": _cmd_publish,
    "hotfix": _cmd_hotfix,
    "abandon": _cmd_abandon,
}


async def _cmd_doof(repos, ack, respond, command, context):
    parts = command["text"].split(None, 1)
    if not parts or parts[0] not in _SUBCOMMANDS:
        await ack()
        await respond(_USAGE)
        return
    subcommand, *rest = parts
    sub_command = dict(command, text=rest[0] if rest else "")
    await _SUBCOMMANDS[subcommand](repos, ack, respond, sub_command, context)


def create_app():
    repos = config.load_repos_config()
    app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])
    app.command("/doof")(partial(_cmd_doof, repos))
    app.action("promote_production")(partial(_handle_promote_button, repos))
    return app


async def main():
    app = create_app()
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    await handler.start_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
