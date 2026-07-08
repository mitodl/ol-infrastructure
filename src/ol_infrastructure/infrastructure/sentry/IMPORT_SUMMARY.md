# Sentry import summary

Organization: `mit-office-of-digital-learning`

## Generated Pulumi resources

- `code_mapping`: 109
- `dashboard`: 20
- `github_repository`: 176
- `issue_alert`: 4
- `key`: 20
- `member`: 44
- `organization`: 1
- `project`: 19
- `team`: 15

## Live inventory counts

- teams: 15
- projects: 19
- members: 44
- repositories: 181
- code mappings: 109
- dashboards: 21
- keys: 20
- issue alerts: 10
- metric alerts: 6
- plugins: 0

## Warnings and provider caveats

- Skipped non-GitHub repository mitodl/mit-open-bk with provider unknown.
- Skipped non-GitHub repository mitodl/realistic-mm-users with provider unknown.
- Skipped non-GitHub repository mitodl/redash with provider unknown.
- Skipped non-GitHub repository mitodl/response-map with provider unknown.
- Skipped non-GitHub repository mitodl/testinfra with provider unknown.
- Skipped dashboard Learn Performance (140561): pulumiverse-sentry 0.0.9 only supports dashboard widget types ['discover', 'issue', 'metrics'], found ['error-events', 'spans'].
- Skipped issue alert mitxonline/10002327347 (Critical - Notify Rootly, Warning - Notify Rootly): rule is driven exclusively by a Sentry-app integration action (NotifyEventSentryAppAction) with actionMatch=null. Sentry's classic Rules API accepts GET but rejects PUT with 404 for these rules, so pulumiverse-sentry 0.0.9 cannot manage them.
- Skipped issue alert open-next/10002210775 (Notify OpsGenie via Opsgenie): rule is driven exclusively by a Sentry-app integration action (NotifyEventSentryAppAction) with actionMatch=null. Sentry's classic Rules API accepts GET but rejects PUT with 404 for these rules, so pulumiverse-sentry 0.0.9 cannot manage them.
- Skipped issue alert openedx-mitxonline/10002210773 (Notify Rootly): rule is driven exclusively by a Sentry-app integration action (NotifyEventSentryAppAction) with actionMatch=null. Sentry's classic Rules API accepts GET but rejects PUT with 404 for these rules, so pulumiverse-sentry 0.0.9 cannot manage them.
- Skipped issue alert openedx-mitxpro/10002210774 (Notify OpsGenie via Opsgenie): rule is driven exclusively by a Sentry-app integration action (NotifyEventSentryAppAction) with actionMatch=null. Sentry's classic Rules API accepts GET but rejects PUT with 404 for these rules, so pulumiverse-sentry 0.0.9 cannot manage them.
- Skipped issue alert openedx-residential/10002327352 (Critical - Notify Rootly, Warning - Notify Rootly): rule is driven exclusively by a Sentry-app integration action (NotifyEventSentryAppAction) with actionMatch=null. Sentry's classic Rules API accepts GET but rejects PUT with 404 for these rules, so pulumiverse-sentry 0.0.9 cannot manage them.
- Skipped issue alert xpro/10002210772 (Notify OpsGenie via Opsgenie): rule is driven exclusively by a Sentry-app integration action (NotifyEventSentryAppAction) with actionMatch=null. Sentry's classic Rules API accepts GET but rejects PUT with 404 for these rules, so pulumiverse-sentry 0.0.9 cannot manage them.
- Skipped metric alerts: pulumiverse-sentry 0.0.9 cannot refresh live Sentry metric alerts whose actions contain numeric targetIdentifier values (provider JSON unmarshal error).
- Dashboard widget IDs and query IDs are computed-only in the provider and are omitted from generated code.
- Issue alert action/filter/condition maps are ignored after import because Sentry's issue-alert API/provider refresh currently normalizes imported rule body lists in a way that would otherwise cause destructive drift. `actionMatch` is still managed and null live values are generated as `any`.

## Import command

From `src/ol_infrastructure/infrastructure/sentry`:

```bash
pulumi stack select Production
pulumi import --file sentry_imports.json --preview-only
pulumi import --file sentry_imports.json
pulumi preview --refresh --diff
```

The import file contains resource IDs only, not Sentry token values or DSNs.
