"""Slack Bolt async app — all command and action handlers for the release bot."""

import asyncio
import logging
import os

from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
from slack_bolt.async_app import AsyncApp

import concourse
import config
import github

log = logging.getLogger(__name__)

repos = config.load_repos_config()
app = AsyncApp(token=os.environ["SLACK_BOT_TOKEN"])


@app.command("/release")
async def cmd_release(ack, respond, command):
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


@app.command("/release-notes")
async def cmd_release_notes(ack, respond, command):
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
        await respond(f"*Release notes for `{app_name}`*\n_(no commits since last release)_")
        return
    lines = [f"• `{c['sha'][:8]}` {c['message'].splitlines()[0]}" for c in commits]
    await respond(f"*Release notes for `{app_name}`*\n" + "\n".join(lines))


@app.command("/release-status")
async def cmd_release_status(ack, respond, command):
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


@app.command("/promote")
async def cmd_promote(ack, respond, command, context):
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
            comment=f"Promoted to Production by <@{user}> via `/promote`.",
        )
    except Exception:
        log.exception("Failed to promote %s", app_name)
        await respond(f"❌ Failed to promote `{app_name}`.")
        return
    await respond(
        f"✅ `{app_name}` {issues[0]['title']} promoted. Production deploy triggered."
    )


@app.command("/publish")
async def cmd_publish(ack, respond, command):
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


@app.command("/hotfix")
async def cmd_hotfix(ack, respond, command):
    await ack()
    parts = command["text"].split(None, 1)
    if len(parts) != 2:  # noqa: PLR2004
        await respond("Usage: `/hotfix <app> <commit-hash>`")
        return
    app_name, commit_hash = parts
    if app_name not in repos:
        await respond(f"Unknown app `{app_name}`. Known apps: {', '.join(repos)}")
        return
    cfg = repos[app_name]
    try:
        build_url = await concourse.trigger_job(
            cfg.pipeline, "create-hotfix", commit_hash=commit_hash
        )
    except Exception:
        log.exception("Failed to trigger hotfix for %s", app_name)
        await respond(f"❌ Failed to trigger hotfix for `{app_name}`.")
        return
    await respond(
        f"🔧 Hotfix triggered for `{app_name}` at `{commit_hash[:8]}`. Build: {build_url}"
    )


@app.action("promote_production")
async def handle_promote_button(ack, body, say):
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


async def main():
    handler = AsyncSocketModeHandler(app, os.environ["SLACK_APP_TOKEN"])
    await handler.start_async()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
