# Sentry import summary

Organization: `mit-office-of-digital-learning`

## Generated Pulumi resources

- `code_mapping`: 109
- `dashboard`: 20
- `github_repository`: 176
- `issue_alert`: 4
- `key`: 20
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

- Skipped 44 organization members: membership is intentionally left out of Pulumi management and continues to be administered directly in Sentry.
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

Re-running `bin/import-sentry-config generate` regenerates `__main__.py`,
`sentry_imports.json`, and this file together from live Sentry
configuration, then `pulumi import --file sentry_imports.json` followed by
`pulumi preview --refresh --diff` applies any newly discovered resources.

## Hand-authored exceptions

- `project_ol_analytics_api` / `key_ol_analytics_api`: added by hand (not
  produced by `bin/import-sentry-config`) because the ol-analytics-api Sentry
  project does not exist live yet -- there is nothing to import. `pulumi up`
  creates both directly; this is an ordinary new-resource create, not an
  import. Named to match the generator's convention (`project_<slug>`,
  `key_<project_slug>`, sans the live numeric key id the generator normally
  suffixes onto key names) so that once this project is live, a future
  `bin/import-sentry-config` run converges onto the same resource names
  instead of creating parallel ones -- confirm the regenerated key name still
  matches before accepting that diff (it will include the live key id, e.g.
  `key_ol_analytics_api_default_<id>`, so this hand-picked name may need a
  matching `pulumi state rename` at that point). The `ol_analytics_api_sentry_dsn`
  stack output is also hand-added and not part of the generator's output
  template.

- `project_dagster` / `key_dagster`: added by hand for the same reason -- the
  Dagster Sentry project does not exist live yet, so there is nothing to
  import and `pulumi up` creates both. One project serves all environments and
  all ten code locations; they are separated by the SDK's `environment` tag and
  a `dagster_code_location` tag rather than by separate projects. The
  `dagster_sentry_dsn` stack output is consumed by the Dagster application
  stack, which writes it to Vault at `secret-data/dagster/sentry`. The same
  naming-convergence caveat as above applies to `key_dagster`.
- `*_sentry_dsn` stack outputs for every other generated `key_*` resource
  (see ol-infrastructure#5004): hand-added `pulumi.export(...)` calls at the
  end of the file, exposing each project's DSN as `<project_slug>_sentry_dsn`
  (name-suffixed for projects with more than one key, e.g.
  `odl_video_service_eternal_mink_sentry_dsn`) so consuming Pulumi stacks can
  read the DSN via `sentry_stack.require_output(...)` instead of a
  hard-coded/SOPS/Vault secret. Like the `ol_analytics_api` exception, these
  are not part of the generator's output template -- if `bin/import-sentry-config`
  is re-run and a key's generated variable name changes (e.g. its numeric
  suffix), update the matching export line by hand.
- `project_airbyte` / `key_airbyte_default_*`, `project_unified_ecommerce` /
  `key_unified_ecommerce_default_*`, `project_python` / `key_python_default_*`,
  and `project_sandbox` / `key_sandbox_default_*`: removed by hand post-import
  (unused, zero Sentry events/issues in any of them). If `bin/import-sentry-config`
  is re-run against a `sentry_imports.json` that still lists these live
  projects, drop them from the import file first or the regenerated code will
  recreate the resource blocks. All eight resources (4 projects + 4 keys)
  carried `protect=True` via the file-wide `sentry_opts`, which would have
  made `pulumi up` fail on these deletes with "cannot be deleted because it
  is protected" -- each was unprotected by hand ahead of merge with
  `pulumi state unprotect '<urn>'` (e.g.
  `urn:pulumi:default::ol-infrastructure-sentry::sentry:index/sentryProject:SentryProject::project_airbyte`),
  confirmed clean with a subsequent `pulumi preview` (4 updates, 8 deletes,
  no unexpected replacements).
- `project_ocw_next` / `key_ocw_next_default_*`: the live Sentry project's
  `name`/`slug` were hand-changed from `ocw-next` to `ocw-site` (matching the
  `ocw_site` application) without renaming the Pulumi resource identifiers,
  so the update applies in place rather than replacing the resource. The
  hand-added export is `ocw_site_sentry_dsn`. A future `bin/import-sentry-config`
  run will regenerate `name`/`slug` back to whatever the live project is
  named at that point -- expect it to match `ocw-site` unless it's renamed
  again live.
- `project_open_next` / `key_open_next_default_*`: the live Sentry project's
  `name`/`slug` were hand-changed from `open-next` to `mit-learn` (without
  renaming the Pulumi resource identifiers, same in-place-update rationale as
  `project_ocw_next` above). `open` is the legacy `open-discussions`/`mit_open`
  Heroku deployment (culprits are old `/api/v0/...` routes and old task
  modules like `search.tasks.*`, `course_catalog.tasks.*`); `open-next` is the
  current MIT Learn stack as a whole -- both the k8s `mit_learn` Django
  backend (`/api/v1/learning_resources/...`, `vector_search.tasks.*`, SCIM)
  and the `mit_learn_nextjs` frontend share this one project, the same way
  `project_dagster` covers all of Dagster's code locations rather than
  splitting by app. The hand-added export is `mit_learn_sentry_dsn`. A future
  `bin/import-sentry-config` run will regenerate `name`/`slug` back to
  whatever the live project is named at that point -- expect it to match
  `mit-learn` unless it's renamed again live.
- `project_witan` / `key_witan`: added by hand, same reason as
  `project_ol_analytics_api`/`project_dagster` -- the witan Sentry project
  does not exist live yet, so `pulumi up` creates both. One project covers
  CI/QA/Production, distinguished by the SDK's `environment` tag rather than
  by separate projects. The hand-added export is `witan_sentry_dsn`, consumed
  by the witan application stack, which writes it to Vault at
  `secret-operations/witan/sentry`. The same naming-convergence caveat as
  `project_ol_analytics_api` applies: if this project is later created live
  and `bin/import-sentry-config` is re-run, diff the regenerated
  `key_witan_default_*` block against this one before accepting it -- a
  `pulumi state rename` may be needed to converge onto the live key's id.
