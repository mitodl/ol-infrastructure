# GCP external-grant register

Status: tooled; production and QA clusters probed 2026-08-24. Incomplete — the
edx.org grants and `ol-eng-library-platform@` are still outstanding.
Opened 2026-08-24 for task
`tk-enumerate-external-google-product-grants-per-cre-070a83` (p0).

Companion to `gcp-service-account-consumer-map.md`, which answers *what calls this
credential*. This document answers the other half: **what has been granted TO this
credential, in products outside GCP** — and therefore what has to be re-issued by
hand when the credential is replaced.

## Why this gates every migration step

Service accounts, API keys and reCAPTCHA keys **cannot be moved between GCP
projects**. No API exists for it. Consolidating a credential into `mitol01`
therefore means creating a *replacement*, which produces a new service-account
email or a new key string. Every grant that named the old identity stops working
at that moment and has to be re-issued against the new one.

So the migration cannot be sequenced until the grants are known. A credential
whose grants are unknown cannot be safely re-homed, no matter how well its
*consumers* are understood.

## The premise that turned out to be half wrong

The task was filed on the understanding that these grants cannot be enumerated
programmatically — that each had to be walked in its own product's admin UI, some
of them by people outside OL.

The first half holds: **no GCP API can find them**, because the grants do not live
in GCP. They live in Drive, Sheets, Analytics, BigQuery and YouTube.

The second half does not. Each of those products has an API that answers "what can
I see?" — and a service-account key can mint a token for those products' scopes.
Asking each product *as the credential itself* inverts the problem from "audit
every product's admin UI" into "mint one token per credential and ask five
questions". `bin/gcp-external-grants` does exactly that.

This does not eliminate the manual walk. It reduces it to the cases the API
genuinely cannot answer, listed under "What still needs a human" below.

## Tooling

```
bin/gcp-external-grants probe-all --markdown            # every credential; see below
bin/gcp-external-grants scopes                          # what each probe asks, and why
bin/gcp-external-grants probe --key-file sa.json
bin/gcp-external-grants probe --vault MOUNT/PATH[:FIELD] --json
bin/gcp-external-grants probe --heroku APP:VAR --json
bin/gcp-external-grants probe --gcloud                  # the active human identity
```

Read-only: every call is a GET, with read-only scopes. The tool never prints, logs
or writes credential material — only the `client_email` the credential publishes.

What it asks per credential:

| Probe | API | Answers |
|---|---|---|
| `shared_drives` | `drive.drives.list` | Shared Drives the credential is a member of, and at what level |
| `shared_files` | `drive.files.list?q=sharedWithMe` | Individual files/folders shared with it, and who owns them |
| `analytics` | `analyticsadmin.accountSummaries.list` | GA4 accounts/properties it can read |
| `bigquery` | `bigquery.projects.list` | **Every project it holds BigQuery access in, including third parties' own projects** |
| `youtube` | `youtube.channels.list?mine=true` | Channels it is authorised against |

Effective access is read from each item's `capabilities`, not `permissions.list`.
`capabilities` comes back inline with the listing, so it costs no extra call per
item, and it reports what the credential can actually do — including access
inherited from a parent folder or arriving via group membership, which a
permission entry naming the credential would not show. (`permissions.list` is also
believed to need more than reader on the item, which would make it fail exactly
where the answer matters most; **unconfirmed** — do not repeat it as fact.)

The BigQuery probe is the one that earns the tool. `bigquery.projects.list` returns
projects the *caller* can reach, so a third party's grant into their own project —
otherwise discoverable only by asking them — shows up directly.

### Verification performed, 2026-08-24

The probe path (token acquisition → HTTP → classification → report) was exercised
end-to-end against two live gcloud identities. `bigquery.projects.list` returned 13
projects for `mitx.devops@gmail.com`, all correctly classified as OL-owned. The
Drive/Analytics/YouTube probes returned `403 insufficient authentication scopes`,
which is the expected and correct result for a gcloud-minted token: gcloud issues
`cloud-platform` scope only.

Credential loading and grant classification are covered by
`tests/bin/test_gcp_external_grants.py` (26 tests), which pin all three secret
shapes, the escaped-PEM case, and every branch of the ownership rules below.

The service-account path has since been exercised against real keys in both the
production and QA Vault clusters — see Findings.

### The classification rule, and why it is not the obvious one

**Ownership is judged by the project's PARENT, never by whether it appears in
`gcloud projects list`.** That call returns every project an identity can *see*,
which includes third-party projects OL has merely been **granted into** — the exact
population this tool exists to find. Keying on visibility launders those grants
into "internal" and drops them from the migration plan. This was not hypothetical:
see "Two corrections to the tool" below.

A project is OL's when its parent matches one the credentials under test live in:
no parent at all (every legacy gmail-estate project), or the MIT org folder the
migration targets. A project that cannot be described by any OL identity is third
party — OL does not administer what it cannot read metadata for.

The same rule covers service-account grantors, which cannot be judged by domain:
**every** project's service accounts live under `*.iam.gserviceaccount.com`, a
third party's included, so the project id embedded in the address is routed through
the parent check. Human addresses fall back to the domain.

Where ownership cannot be determined the tool reports *unknown* rather than
guessing, and every run prints the parent set it used.

## What to run, per credential

Vault paths and field names below were read out of the code that consumes them
(`k8s_secrets.py`, `dagster/__main__.py`, and the Dagster code locations in
`ol-data-platform`), not guessed. **The field names are not uniform and the KV
version is not uniform** — three shapes occur, and the tool handles all three:

| Consumer | Mount | KV | Path | Field |
|---|---|---|---|---|
| xPro Sheets | `secret-xpro` | v1 | `google-sheets` | `service_account_creds` |
| OCW Studio (prod **and** RC/QA — same path) | `secret-ocw-studio` | v2 | `collected` | `google` → `drive_service_json` |
| Dagster `canvas` | `secret-data` | v1 | `pipelines/google-service-account` | *(whole body is the key)* |
| Dagster `edxorg`/`legacy_openedx` | `secret-data` | v1 | `pipelines/edx/org/gcp-oauth-client` | *(whole body is the key)* |
| `ol-eng-library-platform@` | — | — | **not in Vault** | Heroku `GOOGLE_APPLICATION_JSON` |

### The whole run, in one command

`probe-all` carries the table above as a manifest, so it takes no arguments. It
walks the credentials in the order listed, reports progress on stderr and the
findings on stdout, and **never skips a credential silently** — one that fails to
load gets a `NOT ENUMERATED` row carrying the reason, because a row that is simply
absent reads downstream as "asked, found nothing".

```sh
export VAULT_ADDR=https://vault-production.odl.mit.edu

# Findings table rows, ready to paste into this document.
bin/gcp-external-grants probe-all --markdown | tee /tmp/grants-production.md

# Or keep the raw output for the record.
bin/gcp-external-grants probe-all --json > /tmp/grants-production.json
```

**Run it a second time against QA**, or you will not have probed
`ocw-studio-rc@` at all:

```sh
VAULT_ADDR=https://vault-qa.odl.mit.edu \
  bin/gcp-external-grants probe-all --markdown --skip-heroku
```

`--skip-heroku` on the second run because `ol-eng-library-platform@` is a single
credential reached through Heroku, not a per-environment one — probing it twice
gains nothing. The Heroku step needs the `heroku` CLI logged in; without it that
one credential reports a load failure and the other four still run.

Individual credentials can still be probed directly when you only need one:

```sh
bin/gcp-external-grants probe \
  --vault secret-data/pipelines/edx/org/gcp-oauth-client --json
bin/gcp-external-grants probe \
  --heroku ol-eng-library:GOOGLE_APPLICATION_JSON --json
```

Three traps in the above, each of which cost time to find:

- **`ocw-studio-production@` and `ocw-studio-rc@` share one Vault path.** There is
  no environment prefix; the path, mount and field are identical. The two SAs are
  distinguished only by which Vault cluster answers. Running the same command twice
  against the same cluster probes the same credential twice and looks like agreement.
- **`ol-eng-library-platform@` is not in Vault at all.** No `OLVaultK8SSecret`, no
  vault-agent template, no policy grant, no SOPS entry. It reaches its three Heroku
  apps as a config var and nothing else. This is itself a migration finding: the
  credential with the estate's highest Drive traffic (592k calls/30d) is stored
  outside every secret-management path this team operates.
- **The `edxorg` credential is stored with non-standard field names** — `url` and
  `cert_url` where a service-account JSON says `auth_uri` and
  `client_x509_cert_url`. Harmless for this tool, which signs with `client_email`
  and `private_key` only, but anything else splatting that secret into a Google
  client should expect it.

Two of these have a specific question attached that the probe should settle:

- **`ol-data-platform-production@`** shows 2,540 Sheets `GetSpreadsheet` calls/30d
  against a source the consumer map lists as unidentified. `shared_files` enumerates
  every sheet shared with it, which should name it.
- **`ol-eng-library-platform@`** is credited with two Shared Drives, but its 592k
  `Files.List` calls/30d serve three Heroku apps. `shared_drives` will show whether a
  third drive is involved.

## Findings

First production run, 2026-08-24, against `vault-production`. **Partial** — see
"What this run did not cover" below before treating any absence as an answer.

| Credential | Product | Resource id | Resource name | Access | Grantor | Third party? |
|---|---|---|---|---|---|---|
| `ol-data-platform-production@` | BigQuery | `mitx-residential-pipeline-main` | mitx-residential-pipeline-main | project-level | not describable by any OL identity | **YES** |
| `ol-data-platform-production@` | BigQuery | `mitir-mitx-surveys` | MITIR MITx Surveys | project-level | not describable by any OL identity | **YES** |
| `ol-data-platform-production@` | BigQuery | `mitx-pipeline-main-dc29` | mitx-pipeline-main | project-level | `folder/249626760288` | **YES** (corrected) |
| `xpro-coupon-requests-productio@` | Drive (folder) | `12FeE1rh0iGQqsIQMvuKiEs07qpZNX541` | xPRO Enrollments | content-manager/writer | **`pdpinch@gmail.com`** | **YES** |
| `ocw-studio-production@` | Drive (Shared Drive) | `0AIZerpz9jimTUk9PVA` | OCW Content | organizer | Shared Drive organizer | no |
| `ocw-studio-production@` | Drive (Shared Drive) | `0AErNBMZMmOz3Uk9PVA` | **OL Engineering (ARCHIVED)** | organizer | Shared Drive organizer | no |
| `ol-data-platform-qa@` | BigQuery | `mitx-residential-pipeline-main` | mitx-residential-pipeline-main | project-level | not describable by any OL identity | **YES** |
| `ol-data-platform-qa@` | BigQuery | `mitir-mitx-surveys` | MITIR MITx Surveys | project-level | not describable by any OL identity | **YES** |
| `ol-data-platform-qa@` | BigQuery | `mitx-pipeline-main-dc29` | mitx-pipeline-main | project-level | `folder/249626760288` | **YES** |
| `xpro-coupon-requests-testing@` | Drive (Shared Drive) | `0ADY3FaGtq2jvUk9PVA` | Open Learning Engineering | content-manager/writer | Shared Drive organizer | no |
| `xpro-coupon-requests-testing@` | Drive (folder) | `1FNLXfLSC4IATz8zKIjitx0GAxeow0NCO` | Sheets API Testing | content-manager/writer | `gwsidebottommit@gmail.com` | **YES** |
| `xpro-coupon-requests-testing@` | Drive (presentation) | `1G5qxWqEx-PPcnypXes12YDCUl_4Qe8GkU3LRrBVtJ1k` | Compliance and Refusal Tech Talk | reader | `ptylkin@gmail.com` | **YES** |
| `xpro-coupon-requests-testing@` | Drive (presentation) | `1X8gRX0QNQbPmJtkdS31CjursZVxS7kd9bg57wSfX4XQ` | Frontend tooling | reader | `christopher.chudzicki@gmail.com` | **YES** |
| `xpro-coupon-requests-testing@` | Sheets | (13 "Enrollment Codes …" sheets + "(RC) Enrollment Code Requests") | | content-manager/writer | Shared Drive resident | no |
| `ocw-studio-rc@` | Drive (Shared Drive) | `0AErNBMZMmOz3Uk9PVA` | OL Engineering (ARCHIVED) | organizer | Shared Drive organizer | no |
| `ocw-studio-rc@` | Drive (folder) | `1H4HCvbmY7v5YZFeqSlbCI1TFC5MXTMY4` | OCW Studio RC Website Uploads | content-manager/writer | Shared Drive resident | no |

### What this run settled

- **The Shared Drive collision is resolved.** `0AErNBMZMmOz3Uk9PVA` is **"OL
  Engineering (ARCHIVED)"**. The consumer map labels it "ol-eng-library" under
  `ol-eng-library-platform@` and "Pulumi.QA" under `ocw-studio-rc@`
  (`gcp-service-account-consumer-map.md:64`, `:68`). **Both labels are wrong**, and
  the drive is *archived* while a production credential holds **organizer** on it.
  Note it answered under `ocw-studio-production@`, not `-rc@`.
- **A production credential depends on a personal Gmail account.** The xPro
  enrollments folder is owned by `pdpinch@gmail.com`. `xpro-coupon-requests-productio@`
  holds content-manager/writer on it by that person's grant. If that account lapses
  or is cleaned up, the grant goes with it — and re-issuing after consolidation
  requires them personally. This is a live single point of failure, not just a
  migration cost.
- **Two genuinely external BigQuery grants**, neither visible to any OL gcloud
  identity: `mitx-residential-pipeline-main` and `mitir-mitx-surveys` (MIT
  Institutional Research). Re-issuing these needs their owners.

### What this run did NOT cover

- **`ol-data-platform-qa@` was never probed.** Both
  `secret-data/pipelines/edx/org/gcp-oauth-client` and
  `secret-data/pipelines/google-service-account` returned
  `ol-data-platform-production@`, so the manifest's first two entries hit one
  identity and its rows appeared twice. **The edx.org BigQuery/GCS grants — the
  highest-value item in this whole exercise — remain unenumerated.** Resolve where
  `ol-data-platform-qa@`'s key actually lives before rerunning. `probe-all` now
  warns when two sources resolve to one identity.
- **`ocw-studio-rc@` was not probed**; the run only touched `vault-production`.
- **`ol-eng-library-platform@` produced no rows** — the Heroku step needs the
  `heroku` CLI logged in.
- **No Analytics or YouTube grants appeared for any credential.** Not yet
  meaningful: those probes return 403 when the credential holds nothing *and* when
  the scope is refused, and the two are not distinguished in this output.

### QA-cluster run, 2026-08-24

Run against `vault-qa`, which is what reaches `ol-data-platform-qa@`,
`xpro-coupon-requests-testing@` and `ocw-studio-rc@`.

**`ol-data-platform-qa@` holds exactly the same three BigQuery projects as
`ol-data-platform-production@`**: `mitx-residential-pipeline-main`,
`mitx-pipeline-main-dc29`, `mitir-mitx-surveys`. Two consequences:

1. **No edx.org-owned dataset appeared for either credential.** The consumer map
   attributes 48,258 BigQuery calls/30d to "edx.org-owned BigQuery datasets / GCS
   bucket". The three projects found are all `mitx-*`/`mitir-*` pipeline projects.
   Either the map's "edx.org" attribution is imprecise and these *are* the datasets
   (the `-pipeline-main` naming is edX's own convention), or a dataset-level grant
   exists that project-level enumeration does not reach. **Unresolved** — do not
   record the edx.org grants as enumerated on the strength of this.
2. **`ocw-studio-rc@` also holds organizer on `0AErNBMZMmOz3Uk9PVA`**
   ("OL Engineering (ARCHIVED)"), same as `ocw-studio-production@`. It does *not*
   hold "OCW Content" — that is production-only, so the prod/rc split is real.

**`xpro-coupon-requests-testing@` has accumulated 16 grants, several accidental.**
Beyond the expected Shared Drive "Open Learning Engineering" and the enrollment-code
sheets, it holds:

| Resource | Access | Owner |
|---|---|---|
| "Compliance and Refusal Tech Talk" (presentation) | reader | `ptylkin@gmail.com` |
| "Frontend tooling" (presentation) | reader | `christopher.chudzicki@gmail.com` |
| "Sheets API Testing" (folder) | content-manager/writer | `gwsidebottommit@gmail.com` |

Two slide decks shared with a **service account** are almost certainly misdirected
shares — someone typed the SA address into a share dialog. Harmless individually,
but they show the SA address circulating as if it were a person's, and each is a
grant that has to be considered at migration time. Note all three owners are
personal Gmail accounts, consistent with the estate-wide pattern.

### Two corrections to the tool, both forced by these runs

**1. Visibility is not ownership — the first fix made this worse.**

The production run reported `mitx-pipeline-main-dc29` third party. That was
*correct*, and an intermediate "fix" wrongly overrode it. The reasoning chain:

- Ownership was first derived from `gcloud projects list` for the *active* account.
  As `tmacey@mit.edu` that returns only `mitol-engineering` and `mitol01`, so the
  entire legacy estate classified as external.
- The obvious repair — union across all credentialed accounts — reclassified
  `mitx-pipeline-main-dc29` as OL-owned. **Also wrong, and worse.**
  `gcloud projects list` returns every project an identity can *see*, which
  includes third-party projects OL has been **granted into**. Those grants are the
  population this tool exists to find, so keying ownership on visibility launders
  them into "internal" and drops them from the migration plan. The grant shows up
  as proof of ownership.
- Confirmed directly: `mitx-pipeline-main-dc29` sits in `folder/249626760288`,
  which `mitx.devops@gmail.com` cannot even describe. It appears in `projects list`
  solely because of the viewer grant recorded in
  `tk-grant-mitx-devops-gmail-com-viewer-access-to-mit-0caa08`.

Ownership is now judged by **project parent**, which has no such failure mode:

| Project | Parent | Verdict |
|---|---|---|
| `ocw-studio-production` | *none* | OL's — every legacy project looks like this |
| `mitol01` | `folder/551004127831` | OL's — the migration target |
| `mitx-pipeline-main-dc29` | `folder/249626760288` | third party |
| `mitir-mitx-surveys` | not describable | third party |

The owned-parent set is seeded from the credentials **being probed**, unioned
across the run, so it does not move with `gcloud config set account` and a
legacy-estate credential does not judge `mitol01` external. `--owned-parent
folder/<id>` and `--owned-project <id>` pin anything else.

**2. Shared-Drive-resident files reported "unknown owner".**

Files inside a Shared Drive carry no `owners` field — the drive owns them. That
rendered a dozen enrollment-code sheets as "unknown owner / unknown", burying the
genuinely unknown rows in noise. The probe now resolves `driveId` against the
drives the credential is a member of and names the drive as grantor.

### Known before probing

Carried over from the consumer map. Every row needs confirmation — these were read
out of application config and traffic shape, not out of the granting product.

| Credential | Product | Resource id | Source of claim |
|---|---|---|---|
| `ol-eng-library-platform@` | Drive (Shared Drive) | `0AErNBMZMmOz3Uk9PVA` (ol-eng-library) | consumer map |
| `ol-eng-library-platform@` | Drive (Shared Drive) | `0AAx-gQtx1vVKUk9PVA` (mit-open-library) | consumer map |
| `ocw-studio-production@` | Drive (Shared Drive) | `0AIZerpz9jimTUk9PVA` (`DRIVE_SHARED_ID`) | app config |
| `ocw-studio-rc@` | Drive (Shared Drive) | `0AErNBMZMmOz3Uk9PVA` (Pulumi.QA) | app config |
| `ol-data-platform-production@` | Sheets | `13AoothEhEvWs2cJEEfZETm7E6h3-ZY4tD11KX_ARe1A` gid `1472315099` | canvas code location sensor |
| `xpro-coupon-requests-productio@` | Sheets + Drive folder | `COUPON_REQUEST_SHEET_ID`, `DRIVE_OUTPUT_FOLDER_ID` | app config |
| `ol-data-platform-qa@` | BigQuery + GCS | edx.org-owned datasets and bucket | 48,258 BQ calls/30d |

Note the collision worth resolving during migration: `0AErNBMZMmOz3Uk9PVA` is
recorded against **both** `ol-eng-library-platform@` (as "ol-eng-library") and
`ocw-studio-rc@` (as the QA drive). At most one of those labels is right. The
`shared_drives` probe resolves it, since it reports the drive's actual name.

## What still needs a human

The probes do not cover these, and no amount of tooling will make them cover them.

1. **Generic OAuth 2.0 clients.** No create, update or even *list* API exists. The
   three live ones are known only because they showed traffic; dormant ones cannot be
   inventoried at all. Their grants must be walked in the Console, project by project.
2. **Google Ads links.** Not reachable with a read-only service-account token.
3. **YouTube channel permissions.** `channels.list?mine=true` answers for an OAuth
   client, not for the human-managed channel permissions behind OCW Studio publishing.
4. **Grants made to a Google *group* the credential belongs to.** The probe sees the
   effective access but not that it arrives via group membership, so revoking the
   credential's own grant would not remove it — and re-issuing means adding the new
   identity to the group, not re-sharing the resource.
5. **GCS bucket grants.** There is no cross-project "list buckets I can read" API.
   `ol-data-platform-qa@`'s edx.org bucket read has to be confirmed against the
   consumer's own configuration. Note the consumer map already flags that *no*
   `storage.googleapis.com` traffic appears for it, so its GCS path is doubly
   unconfirmed.

## Two things this register deliberately does not track

**Granted quota is not a grant.** The YouTube quota increases on `ocw-studio-qa`
(210,000/day, 21×) and `mit-open` (100,000/day, 10×) attach to the *project* and do
not follow a consumer anywhere. They will never appear in probe output and must not
be conflated with the grants that do. `mitol01` starts at Google's default
10,000/day, and those increases must be requested and **granted** there before
either consumer moves. Tracked separately as
`tk-request-youtube-quota-increases-on-mitol01-befor-1c7003`.

**Absence is not proof.** An empty probe result means nothing was found, not that
nothing exists. This estate has already produced two triage passes that read a zero
as a death certificate and were wrong both times. Every "no grants found" line the
tool prints says so.
