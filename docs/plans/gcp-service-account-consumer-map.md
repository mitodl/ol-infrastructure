# GCP service-account consumer map

Status: discovery complete for the credential→consumer axis.
Evidence gathered 2026-08-10. Supersedes the key-age triage and refines the
last-authentication triage of 2026-07-21.

## Why this document exists

Task `tk-build-the-gcp-service-account-consumer-map-befor-c3b1af` (p0) blocks the
import-strategy and reparenting design. Two prior triage passes were each wrong:

1. **Key age** (2026-07-21 morning) — declared 12 SAs dead. Wrong in both directions.
2. **Last authentication** (2026-07-21 afternoon) — corrected that to "12 of 16 live".
   Better, but it over-counts: it measures *token minting*, which succeeds even when
   the subsequent API call fails or is never made.

This pass adds the decisive third signal: **actual API request counts, by credential**,
from Cloud Monitoring. That converts "this credential is alive" into "this credential
calls *this API* *this many times*", which is what a migration plan actually needs.

## Method (repeatable)

```
serviceruntime.googleapis.com/api/request_count
  grouped by resource.label.service        # which Google API
              resource.label.credential_id # which SA / OAuth client / API key
              resource.label.method        # which RPC
              metric.label.response_code
  window 2026-07-11 .. 2026-08-10 (30d), ALIGN_SUM / REDUCE_SUM
```

Tooling: **`bin/gcp-credential-usage`** (cyclopts; Monitoring REST v3; needs only
project-level read). It resolves `credential_id` to human names automatically.

```
gcloud config set account <identity that can see the legacy projects>
bin/gcp-credential-usage report --days 30 mit-open mitxpro ocw-studio-qa
bin/gcp-credential-usage report --all-projects --json > usage.json
bin/gcp-credential-usage last-auth engineering-project-management   # regression check
```

`serviceaccount:<uniqueId>` resolves via `gcloud iam service-accounts list`,
`apikey:<uid>` via `gcloud services api-keys list`, and for
`oauth2:<project-number>-<hash>` the numeric prefix is the **owning project's
number** — the only programmatic handle on a generic OAuth client.

**This metric needs no audit-log configuration** — unlike `gcloud logging read`, which
returned nothing useful because data-access audit logs are off by default (and Cloud
Logging API is not even enabled on most legacy projects).

### Caveat that must not be forgotten

**Positive traffic is proof. Zero traffic is not.** Cloud Storage in particular does not
report caller-side `request_count` the way Sheets/Drive/YouTube/BigQuery do, so a GCS-only
consumer can be invisible here. Treat every "no traffic" row below as *unconfirmed*, not
*dead* — the same mistake that key-age triage made. See "Unresolved" at the end.

## The map

### Confirmed live, consumer identified

| Service account | Project | API traffic (30d) | Consumer | External grant |
|---|---|---|---|---|
| `ol-eng-library-platform@` | engineering-project-management | **591,993** Drive `Files.List` | Heroku apps `ol-eng-library`, `mit-open-library`, `mit-open-library-rc` (`GOOGLE_APPLICATION_JSON`) | Shared Drives `0AErNBMZMmOz3Uk9PVA` (ol-eng-library), `0AAx-gQtx1vVKUk9PVA` (mit-open-library) |
| `xpro-coupon-requests-productio@` | mitxpro | **1,057,708** Sheets `GetValues` + 282,101 `UpdateValues` + Drive `Files.Watch`/`Permissions.Create` | xPro coupon/refund/deferral sheets (`DRIVE_SERVICE_ACCOUNT_CREDS`, Vault `secret-xpro` → `google-sheets/service_account_creds`) | Sheet + Drive folder shares (`COUPON_REQUEST_SHEET_ID`, `DRIVE_OUTPUT_FOLDER_ID`) |
| `xpro-coupon-requests-testing@` | mitxpro | 120,318 Sheets `UpdateValues` + 32,179 `GetValues` | same integration, non-prod | same |
| `ocw-studio-production@` | ocw-studio-production | 6,316 Drive `Files.Get`, 276 `List`, 28 `Create` | ocw-studio (`DRIVE_SERVICE_ACCOUNT_CREDS` ← Vault `secret-ocw-studio` `google/drive_service_json`) | Shared Drive `DRIVE_SHARED_ID` = `0AIZerpz9jimTUk9PVA` (Production) |
| `ocw-studio-rc@` | ocw-studio-rc | 11,695 Drive `Files.List`, 6,015 `Get`, 56 `Create`, 9 `Delete` | ocw-studio QA/RC | Shared Drive `0AErNBMZMmOz3Uk9PVA` (Pulumi.QA) |
| `ol-data-platform-qa@` | ol-data-platform | **48,258** BigQuery `TableDataService.List` + `GetTable`/`ListTables`/`InsertJob` | Dagster `edxorg` / `legacy_openedx` code locations (Vault `secret-data/pipelines/edx/org/gcp-oauth-client` → `GCSConnection`) | Read access on edx.org-owned BigQuery datasets / GCS bucket |
| `ol-data-platform-production@` | ol-data-platform | 2,540 Sheets `GetSpreadsheet`, 719 `GetValues` | Dagster `canvas` code location — `canvas_google_sheet_course_id_sensor` (Vault `secret-data/pipelines/google-service-account`) | Sheet `13AoothEhEvWs2cJEEfZETm7E6h3-ZY4tD11KX_ARe1A`, worksheet gid `1472315099`; scopes `spreadsheets` + `drive` |

Note the inversion worth double-checking during migration: the **QA** SA carries the
heavy BigQuery load and the **production** SA does Sheets. That is the opposite of the
naming, and a reparenting plan that assumes otherwise will cut the wrong credential.

### Confirmed live, but NOT a service account

These projects are alive via **API keys or OAuth clients**, which no prior pass counted
because they are invisible to service-account triage. Several are inside projects
currently slated for deletion.

| Credential | Project | API traffic (30d) | Consumer |
|---|---|---|---|
| API key `4cb11841…` "MIT Open Youtube API Key - Production" (created 2019-11-20) | mit-open | **138,000+** YouTube `PlaylistItems.List` + `Videos.List` | mit-learn YouTube ETL — **confirmed**, see below |
| API key `3612c43f…` "MIT Open Youtube API Key - CI" | mit-open | 9,075 `Subscriptions.List` (**+774 HTTP 403**) | mit-learn CI, failing partially |
| API key `0` "Server key 1" (created **2015-07-21**) | **mitx-residential** | **10,688** YouTube `Videos.List` | edxapp MITx Residential video block — **confirmed**, see below |
| OAuth client `254897979571-hcr8sh7…` | **ocw-studio-qa** | 3,668 YouTube `Videos.Update`, 1,478 `List`, 537 caption ops, **248 `Videos.Insert`** | OCW Studio YouTube publishing |
| OAuth clients `941563042481-ml9e3a…`, `941563042481-n3k2n8…` | mitx-online-production | **60,986 + 25,155** Sheets `GetSpreadsheet`/`GetValues` | MITx Online sheets integration (`MITOL_GOOGLE_SHEETS_DRIVE_CLIENT_ID/SECRET`, Vault `secret-mitxonline` `google-sheets/*`) |

### The two YouTube API-key consumers, traced (2026-08-10)

**`mitx-residential` "Server key 1" → edxapp MITx Residential video block.** Chain:

- `secrets_builder.py:117-125` sets `YOUTUBE_API_KEY` for **only** the `mitx` and
  `mitx-staging` env prefixes. No other deployment gets it.
- `Pulumi.mitx.Production.yaml` carries `edxapp:product: residential`. The GCP project
  is named `mitx-residential`. The key dates from 2015-07-21, the era of that deployment.
- In edx-platform, `settings.YOUTUBE_API_KEY` is used in exactly one place —
  `lms/djangoapps/courseware/views/views.py:362` — which calls
  `settings.YOUTUBE['METADATA_URL']` = `https://www.googleapis.com/youtube/v3/videos/`,
  serving the `/courses/yt_video_metadata` endpoint and the video XBlock's
  `yt_video_metadata` handler.
- That URL is precisely `youtube.api.v3.V3DataVideoService.List`, and `Videos.List` is
  the **only** RPC observed in this project. No other YouTube RPC appears.
- Traffic shape over 14 days matches learner/authoring activity rather than a cron:
  ~50/day baseline with a 2,019-call spike on Jul 27–28 (a course import or republish
  revalidating every video block).

**`mit-open` "…Youtube API Key - Production/CI" → mit-learn YouTube ETL.** Chain:

- mit-learn `main/settings_course_etl.py:116` reads `YOUTUBE_DEVELOPER_KEY`;
  `learning_resources/etl/youtube.py:116` passes it as `developerKey=` to the client.
- `main/settings_celery.py:84-87` schedules `update-youtube-videos` →
  `learning_resources.tasks.get_youtube_data` at `crontab(minute=30, hour=8)` (08:30 UTC).
- The hourly traffic confirms it: **29,380 of 69,194 calls in 14 days land in the 09:00
  UTC hour** — 42% in one hour, tapering through 10:00 (9,989) and 11:00 (7,114). That is
  the 08:30 job running.
- The RPC mix (`PlaylistItems.List`, `Videos.List`, `Playlists.List`, `Channels.List`,
  `Subscriptions.List`) is a channel/playlist crawler, matching `youtube_etl(channel_ids=…)`.
- mit-learn has the secret in production, QA and CI, matching the three keys named
  Production / RC / CI. The RC key shows no traffic.

### YouTube quota: effective vs Google's default

Run `bin/gcp-credential-usage quota <projects>`. An effective limit above the default is
an audited grant that **cannot** move to a consolidated project.

| Project | Effective/day | Default/day | |
|---|---|---|---|
| **ocw-studio-qa** | **210,000** | 10,000 | **21× granted — the most valuable quota in the estate** |
| **mit-open** | **100,000** | 10,000 | **10× granted** |
| ocw-studio-production | 10,000 | 10,000 | default |
| ocw-studio-rc | 10,000 | 10,000 | default |
| mitx-residential | 10,000 | 10,000 | default — live consumer, but no grant to protect |
| ovs-youtube-prod | 10,000 | 10,000 | default, and zero traffic |
| **ovs-youtube-qa** | **0** | 10,000 | **quota zeroed — the project cannot call the API at all** |
| odl-video-service | 10,000 | 10,000 | default |

This settles the exception scope. Only **ocw-studio-qa** and **mit-open** hold
non-transferable granted increases. `ovs-youtube-qa`'s effective limit of 0 also explains
its "no traffic" far better than disuse does — it is incapable of making a call, which is
a good reminder that a zero reading always deserves a cause before it becomes a verdict.

### Credential inventory, complete (2026-08-10)

`scratchpad/cred_inventory.py` enumerated every API key and every *live* OAuth client
across all 20 visible projects, 42-day window.

**API keys — 14 total, only 3 in use.** Restrictions are the story:

| Calls (42d) | Project | Key | Created | Restriction |
|---|---|---|---|---|
| 179,167 | mit-open | MIT Open Youtube API Key - Production | 2019-11-20 | youtube |
| 16,206 | **mitx-residential** | **Server key 1** | **2015-07-21** | **NONE** |
| 11,002 | mit-open | MIT Open Youtube API Key - CI | 2019-11-20 | youtube |
| idle | micromasters-153213 | Development | 2016-12-21 | NONE |
| idle | micromasters-153213 | Production | 2016-12-21 | referrers=micromasters… |
| idle | mit-odl-open-discussions-ci / -rc | youtube-api-key | 2019-11-27 | youtube |
| idle | mit-open | MIT Open Youtube API Key - RC | 2019-11-20 | youtube |
| idle | mitx-residential | API key 4, API key 5 | 2017-11-30 | NONE |
| idle | mitx-residential | bi.odl.mit.edu test | 2017-11-30 | referrers=bi.odl.mit… |
| idle | mitx-residential | Geocoding LTI Module | 2016-03-23 | NONE |
| idle | ocw-studio-qa | OCW Studio RC | 2021-10-13 | NONE |
| idle | ovs-youtube-qa | API key 1 | 2019-12-19 | NONE |

Six keys carry **no restriction of any kind**, including the only unrestricted key that is
actually live. Restricting them is worth doing independently of the migration.

**OAuth clients — 3 genuinely live.** (`32555940559.apps.googleusercontent.com` also appears
in the data; that is the gcloud CLI's own public client — our own queries, not a consumer.
Anything reading this data must filter it out.)

| Calls (42d) | Owning project | API | Consumer |
|---|---|---|---|
| 171,614 | mitx-online-production | Sheets | MITx Online sheets integration |
| 52,604 | mitx-online-production | Sheets | same, second client |
| 9,763 | ocw-studio-qa | YouTube | OCW Studio publishing |

Dormant OAuth clients stay invisible — there is no list API. Only Console inspection per
project will find them.

### reCAPTCHA keys — a third credential type, previously unenumerated

**16 reCAPTCHA Enterprise keys across 4 projects**, found via `gcloud recaptcha keys list`.
These are neither service accounts, nor API keys, nor OAuth clients, and no prior inventory
pass looked for them.

| Project | Keys | Domains served |
|---|---|---|
| recaptcha-migrated-075600d5919 | 8 (2018–2021) | mitxonline.mit.edu, bootcamp/bootcamps.mit.edu, xpro.mit.edu, discussions.odl.mit.edu, open.mit.edu |
| **ol-data-platform** | 3 (2023) | **sso.odl.mit.edu, sso.ol.mit.edu** (Keycloak SSO), mit-open-rc |
| **ocw-studio-qa** | 3 (2026-01/02) | **mit.edu, sites.mit.edu, bounty.mit.edu** ("mit-bounty") |
| mit-open | 2 (2026-04) | learn.mit.edu, rc.learn.mit.edu |

All are wired into live application config: `RECAPTCHA_SITE_KEY`/`RECAPTCHA_SECRET_KEY` for
mit_learn, mit_learn_nextjs, mitxonline, xpro and open_discussions, and
`keycloak_realm:recaptcha_site_key`/`recaptcha_secret_key` in
`substructure/keycloak/Pulumi.{CI,QA,Production}.yaml` (the olapps realm also sets
`captcha_domain = "www.recaptcha.net"` in its CSP and `x_frame_options`).

**These keys are invisible to every GCP-side usage signal.** All four projects show *zero*
`recaptchaenterprise.googleapis.com` traffic apart from our own `ListKeys` calls — because
these are reCAPTCHA *classic* keys that Google auto-migrated into Enterprise (hence the
machine-generated project name). Classic verification goes to `www.google.com/recaptcha/`
and never touches the Enterprise API, so it produces no `request_count`, no last-auth, no
metric of any kind. Liveness can only be established from application config. This is the
sharpest example yet of why zero traffic must never be read as dead.

Two placements are wrong and worth fixing independently of the migration:

- **Keycloak SSO's bot protection depends on a credential in `ol-data-platform`**, a
  data-pipeline project owned by a personal gmail account.
- **`ocw-studio-qa` holds `mit-bounty` keys for mit.edu / sites.mit.edu / bounty.mit.edu**,
  created January–February 2026 and unrelated to OCW Studio. That same project also carries
  the estate's largest YouTube quota (210k/day). It is functioning as a junk drawer for two
  unrelated critical things.

**The estate is still growing.** Keys were created in this legacy, personally-owned estate in
2026-01, 2026-02 and 2026-04. Whatever the migration decides, new-credential creation needs to
be pointed at managed projects now, or the target keeps moving.

### Confirmed dead (no traffic, no recent auth)

- `bigquery-redash@mitx-residential` — never authenticated. Retire. (But **not** its project — see below.)
- `bi-odl-mit-edu@open-learning-analytics`, `google-analytics@open-learning-analytics` — never authenticated; project has zero API traffic. Retire project.
- `mitxonline-app-production@mitx-online-production` — last auth 2022-09-22; zero traffic. Retire **the SA only** — the project is very much alive via its OAuth clients.
- All Google-managed `appspot`/`compute` defaults — never authenticated.
- `edx2github` — zero API traffic of any kind.

### Authenticates but does no work — the "zombie GA cohort"

| Service account | Last auth | API traffic (30d) |
|---|---|---|
| `bi-odl-mit-edu@mit-open` | 2026-07-15 (unchanged over 20 days) | none |
| `bi-odl-mit-edu@odl-video-service` | 2026-07-15 (unchanged) | none |
| `video-ci-odl-mit-edu@odl-video-service` | 2026-07-08 (unchanged) | none |
| `video-odl-mit-edu@odl-video-service` | 2026-06-16 (unchanged) | none |
| `micromasters-google-analytics@micromasters-153213` | 2026-07-15 (unchanged) | none |

Re-running Policy Analyzer on 2026-08-10 is a clean controlled test: in the same run,
`ol-eng-library-platform@` **advanced** 2026-07-18 → 2026-08-04, proving the measurement
still works. These five did not move in 20 days.

Corroborating: all four projects have the **legacy** `analytics.googleapis.com` enabled and
**not** `analyticsreporting.googleapis.com` — which is what `odl-video-service`'s
`ui/utils.py:223` actually calls (`build("analyticsreporting", "v4")` on `GA_KEYFILE_JSON`).
Universal Analytics reporting was retired by Google in 2024. The consistent reading is that
these credentials are still *loaded* by running apps (token mint succeeds → last-auth ticks)
while every downstream call fails or is never issued.

Three of the five (`bi-odl-mit-edu@mit-open`, `bi-odl-mit-edu@odl-video-service`,
`micromasters-google-analytics@`) share the identical 2026-07-15 timestamp across three
separate projects. A single credential-validation sweep touching all three is a plausible
explanation and would make even the last-auth signal a false positive here. **Unproven** —
do not act on it without checking what ran that day.

**Verdict: retire, but stage it.** These are the only SAs where retirement is low-risk, and
they are exactly the ones the first triage would have kept. Disable keys before deleting,
and watch for errors for one cycle.

## Owner answers, 2026-08-10

Five open questions closed by the project owner. These narrow the scope substantially.

1. **App Engine workloads are not this team's concern.** No Datastore archival is required.
   Note the distinction that still matters: the App Engine *apps* are out of scope, but two of
   the three projects hosting them also host live non-App-Engine credentials
   (`engineering-project-management` → the Drive SA behind three Heroku apps;
   `mitx-residential` → the YouTube key behind edxapp Residential). Those projects survive
   until their credentials move; only `edx2github` is free to delete outright.
2. **No relation to bounty.mit.edu.** The three `mit-bounty` reCAPTCHA keys in `ocw-studio-qa`
   are someone else's. Do not migrate them; find the owner and hand them back. Note that a
   third party created credentials inside an OL-owned project — worth understanding how.
3. **Dynamic Ideas and Veltiston AI are legitimately one Google Workspace tenant.** The shared
   `client_id`/`dynideas_client_secret` is correct, not a defect.
4. **Universal Analytics is no longer needed.** The zombie GA cohort can be retired outright
   rather than staged, and the dead GA plumbing removed rather than migrated. A GA4 migration,
   if wanted, is a separate project.
5. **ODL Video Service's YouTube sync is largely dormant.** If it is ever needed again,
   provision fresh credentials post-migration. `ovs-youtube-prod` and `ovs-youtube-qa` need no
   preservation — consistent with both holding only default quota, and `ovs-youtube-qa`'s
   effective limit being 0.

## Corrections this forces on existing tasks

1. **`engineering-project-management` must not be deleted.** 592k Drive calls/30d, last auth
   2026-08-04, three live Heroku apps. Resolves the warning on the p0 task. The dead billing
   account is a separate problem to fix, not a deletion signal.
   → blocks `tk-plan-state-preserving-migration-for-app-engine-a-0043b7`

2. **`mitx-residential` must not be deleted.** A 2015 API key is serving 10,688 YouTube
   `Videos.List` calls per 30 days. Its *service accounts* are all dead, which is why every
   SA-centric pass cleared it. The consumer is unidentified.
   → blocks `tk-plan-state-preserving-migration-for-app-engine-a-0043b7`

3. **The YouTube quota exception is scoped to the wrong projects.** It protected
   `ovs-youtube-prod` and `ovs-youtube-qa`. Neither holds a granted quota increase; both
   sit at or below Google's default, and `ovs-youtube-qa`'s effective limit is **0**. The
   only two projects carrying non-transferable audited grants are **`ocw-studio-qa`
   (210,000/day, 21×)** and **`mit-open` (100,000/day, 10×)**. Re-scoped accordingly.
   Note `mitx-residential` is a live consumer but sits at the default, so it belongs in
   the do-not-delete list for its consumer, not in the quota exception.

4. **`ocw-studio-qa` carries production YouTube publishing.** The OAuth client doing
   `Videos.Insert`/`Update`/caption writes belongs to project number 254897979571 =
   `ocw-studio-qa`. A project named `-qa` holding a production-critical, non-transferable
   quota is both a migration hazard and a naming trap.

5. **`mitx-online-production` is alive via OAuth clients, not its SA.** Retiring
   `mitxonline-app-production@` is safe; touching the project is not.

6. **Service accounts are one of FOUR credential types, and the smallest by traffic.** The
   estate holds service accounts, API keys, OAuth clients and reCAPTCHA keys. Four of the
   six highest-traffic credentials are not service accounts, and the reCAPTCHA keys —
   including the ones guarding SSO login — are invisible to every GCP-side usage signal.
   Manageability differs per type and must be designed per type:
   - **Service accounts, IAM, enabled APIs** — fully Pulumi-manageable.
   - **API keys** — Pulumi-manageable (`gcp.projects.ApiKey`). Bring them under IaC *with*
     restrictions; six currently have none.
   - **reCAPTCHA keys** — Pulumi-manageable (`gcp.recaptcha.EnterpriseKey`), but the site
     key is embedded in production frontends, so a change is a coordinated app deploy.
   - **Generic OAuth clients** — not manageable by any API or IaC tool
     (`pf-generic-gcp-oauth-2-0-client-ids-are-not-managea-18a7a7`). Manual-create +
     secret-store, and not even listable, so dormant ones cannot be inventoried at all.

7. **New credentials are still being created in the legacy estate** (2026-01, 2026-02,
   2026-04). Redirecting new-credential creation to managed projects should not wait for the
   migration design, or the scope keeps growing under it.

## Operational findings (not migration-blocking)

- **xPro Sheets is being throttled hard**: 70,707 `GetValues` + 1,320 `GetSpreadsheet` +
  199 `UpdateValues` returned **HTTP 429** in 30 days, against 1.06M successes (~6% of reads).
  Plus 39×503 and 30×500. Worth a look independently of this project.
- **mit-open CI YouTube key gets 774 HTTP 403s** on `Subscriptions.List`.
- **`engineering-project-management`**: 1,234×500 and 327×403 on Drive `Files.List`, and
  4× Datastore `RunQuery` **403** — something is still trying to reach the retired Datastore.

## Unresolved — remaining work

1. ~~Who calls YouTube with `mitx-residential`'s 2015 "Server key 1"?~~ **Resolved and
   confirmed byte-for-byte, 2026-08-10.** edxapp MITx Residential's video block. The
   convergent evidence is in the tracing section above; the direct check has now also been
   done — `sha256(vault read -field=youtube_api_key secret-mitx/edxapp)` equals
   `sha256(gcloud services api-keys get-key-string 0 --project=mitx-residential)`. Same
   credential, no ambiguity left. (Compare hashes rather than values so neither secret is
   printed.)
2. **External grants are still not enumerated** for any SA. The Shared Drive IDs and Sheet
   IDs above are the first concrete handles — every other grant (Analytics properties, Ads
   links, YouTube channel permissions) still has to be walked product by product in each
   product's own admin UI. This remains the largest hidden cost in the migration.
3. **GCS usage is invisible to this method.** `ol-data-platform`'s edxorg pipeline reads an
   edx.org-owned GCS bucket via `GCSConnection`, and no `storage.googleapis.com` traffic
   appears. Confirm separately before assuming anything about GCS-only paths.
4. **`ol-data-platform-production@`'s Sheets source** is unidentified (2,540 calls/30d).
5. **What authenticated three GA SAs on 2026-07-15?** Determines whether the zombie cohort
   is truly zombie or has a real monthly consumer.
6. Vault was not reachable this session (stale token; `vault login -method=oidc` needed), so
   the Vault-side half of the credential map is inferred from `k8s_secrets.py` templates and
   policy files rather than read directly.
