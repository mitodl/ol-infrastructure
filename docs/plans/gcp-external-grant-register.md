# GCP external-grant register

Status: every known credential probed (Vault 2026-08-24; SOPS, Heroku and the
`ol-data-platform` Drive re-run 2026-09-17). What no probe reaches is listed under
"What still needs a human", which now includes access arriving through link
sharing or a group (see "Drive re-run for ol-data-platform").
The "edx.org grants" question is closed: the counterparty is IRx, not edx.org.
Opened 2026-08-24 for task
`tk-enumerate-external-google-product-grants-per-cre-070a83` (p0).

**People's addresses are redacted.** This repo is public. Grantors that are
individuals appear as labels ("personal Gmail A", "MIT staff account F"); a letter
names the same account everywhere it appears. The real addresses are not stored
anywhere in the repo. Re-run the probe for the credential in question to see them,
and do not paste raw `probe` or `--markdown` output into this file: it prints
grantor addresses as-is.

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
bin/gcp-external-grants probe --sops FILE:FIELD --json
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
projects for the legacy devops Gmail account, all correctly classified as OL-owned. The
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

A project is OL's when **a probed credential lives in it** (holding its key in our
own secret store is the strongest evidence available), or when its parent matches
one of those credentials' parents — the MIT org folder the migration targets.
A project that cannot be described by any OL identity is third party: OL does not
administer what it cannot read metadata for.

**"No parent" is not a membership test.** Every legacy gmail-estate project is
standalone, but so is any small third party's — parentage cannot tell them apart.
Treating an empty parent as "OL's" marked *every* standalone project as internal,
which would launder an Emeritus or Global Alumni grant into "no re-issue needed":
the same failure as keying on `gcloud projects list`, wearing a different hat. A
standalone project no probed credential vouches for is now reported **unknown**,
and `--owned-project` pins the ones you can vouch for by hand.

A practical consequence worth knowing: **the more of the estate one run covers, the
better it classifies**, because each credential vouches for its own project. Prefer
`probe-all` over one-off `probe` calls when the classification matters.

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
| Airbyte (3 SAs) | — | — | **not in Vault** | SOPS `src/bridge/secrets/airbyte/data.production.yaml` |

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
| `xpro-coupon-requests-productio@` | Drive (folder) | `12FeE1rh0iGQqsIQMvuKiEs07qpZNX541` | xPRO Enrollments | content-manager/writer | **personal Gmail A** | **YES** |
| `ocw-studio-production@` | Drive (Shared Drive) | `0AIZerpz9jimTUk9PVA` | OCW Content | organizer | Shared Drive organizer | no |
| `ocw-studio-production@` | Drive (Shared Drive) | `0AErNBMZMmOz3Uk9PVA` | **OL Engineering (ARCHIVED)** | organizer | Shared Drive organizer | no |
| `ol-data-platform-qa@` | BigQuery | `mitx-residential-pipeline-main` | mitx-residential-pipeline-main | project-level | not describable by any OL identity | **YES** |
| `ol-data-platform-qa@` | BigQuery | `mitir-mitx-surveys` | MITIR MITx Surveys | project-level | not describable by any OL identity | **YES** |
| `ol-data-platform-qa@` | BigQuery | `mitx-pipeline-main-dc29` | mitx-pipeline-main | project-level | `folder/249626760288` | **YES** |
| `xpro-coupon-requests-testing@` | Drive (Shared Drive) | `0ADY3FaGtq2jvUk9PVA` | Open Learning Engineering | content-manager/writer | Shared Drive organizer | no |
| `xpro-coupon-requests-testing@` | Drive (folder) | `1FNLXfLSC4IATz8zKIjitx0GAxeow0NCO` | Sheets API Testing | content-manager/writer | personal Gmail B | **YES** |
| `xpro-coupon-requests-testing@` | Drive (presentation) | `1G5qxWqEx-PPcnypXes12YDCUl_4Qe8GkU3LRrBVtJ1k` | Compliance and Refusal Tech Talk | reader | personal Gmail C | **YES** |
| `xpro-coupon-requests-testing@` | Drive (presentation) | `1X8gRX0QNQbPmJtkdS31CjursZVxS7kd9bg57wSfX4XQ` | Frontend tooling | reader | personal Gmail D | **YES** |
| `xpro-coupon-requests-testing@` | Sheets | (13 "Enrollment Codes …" sheets + "(RC) Enrollment Code Requests") | | content-manager/writer | Shared Drive resident | no |
| `ocw-studio-rc@` | Drive (Shared Drive) | `0AErNBMZMmOz3Uk9PVA` | OL Engineering (ARCHIVED) | organizer | Shared Drive organizer | no |
| `ocw-studio-rc@` | Drive (folder) | `1H4HCvbmY7v5YZFeqSlbCI1TFC5MXTMY4` | OCW Studio RC Website Uploads | content-manager/writer | Shared Drive resident | no |
| `ol-data-platform-production@` | Sheets | `1Bd5igssvHZBbFpKXo3oSy2o1amOhmdF_Ajup4p8HsFY` | MITx [Entitlements by program] | content-manager/writer | MIT staff account F | no |
| `ol-data-platform-production@` | Sheets | `16RpyKIWqqAs2vlu1BZtfldje5Ft030A6pnMG76ysBWc` | MIT Moments Content Calendar | reader | Shared Drive `0AP-f7PkqZpuxUk9PVA` (not a member) | unknown |
| `ol-data-platform-production@` | Sheets | `13AoothEhEvWs2cJEEfZETm7E6h3-ZY4tD11KX_ARe1A` | Canvas GenAI - Pilot Tracker | read (not listed; found by direct `files.get`) | Shared Drive `0ADY3FaGtq2jvUk9PVA` Open Learning Engineering | no |
| `ol-data-platform-qa@` | Sheets | `16RpyKIWqqAs2vlu1BZtfldje5Ft030A6pnMG76ysBWc` | MIT Moments Content Calendar | reader | Shared Drive `0AP-f7PkqZpuxUk9PVA` (not a member) | unknown |
| `ol-data-platform-qa@` | Sheets | `17yibaaKdsuQtv3T6zr4GnSKUu3SBiqjAD0YkbVbZ5qE` | Copy of Video Content Calendar | content-manager/writer | **personal Gmail E** | **YES** |
| `ol-data-platform-qa@` | Sheets | `18YaTPPzBsm8YVaEYffQarV1IJ_j3EQw0WUw4O5bJmH8` | Test canvas course IDs ingestion | reader | MIT staff account F | no |
| `ol-data-platform-qa@` | Sheets | `13AoothEhEvWs2cJEEfZETm7E6h3-ZY4tD11KX_ARe1A` | Canvas GenAI - Pilot Tracker | read (not listed; found by direct `files.get`) | Shared Drive `0ADY3FaGtq2jvUk9PVA` Open Learning Engineering | no |
| `ol-eng-library-platform@` | Drive (Shared Drive) | `0AAx-gQtx1vVKUk9PVA` | MIT Open | organizer | Shared Drive organizer | no |
| `ol-eng-library-platform@` | Drive (Shared Drive) | `0AErNBMZMmOz3Uk9PVA` | OL Engineering (ARCHIVED) | organizer | Shared Drive organizer | no |
| `ol-eng-library-platform@` | Drive (Shared Drive) | `0ADY3FaGtq2jvUk9PVA` | Open Learning Engineering | organizer | Shared Drive organizer | no |
| `ol-eng-library-platform@` | Drive (folder) | `178l9N6sC1QJ9Mmnt44PHAkki1hYI2dYm` | StakeholderSession2Feedback | reader | **gostudion.com vendor account** | **YES** |
| `ol-eng-library-platform@` | Drive (folder) | `1noY-thS2g1eqnJSqYKYuTfKzCYJ-WWXn` | StakeholderSession3Feedback | reader | **gostudion.com vendor account** | **YES** |
| `ol-eng-library-platform@` | Drive (presentation) | `1G5qxWqEx-PPcnypXes12YDCUl_4Qe8GkU3LRrBVtJ1k` | Compliance and Refusal Tech Talk | reader | personal Gmail C | **YES** |
| `ol-eng-library-platform@` | Drive (presentation) | `1X8gRX0QNQbPmJtkdS31CjursZVxS7kd9bg57wSfX4XQ` | Frontend tooling | reader | personal Gmail D | **YES** |

### What this run settled

- **The Shared Drive "collision" was not one.** `0AErNBMZMmOz3Uk9PVA` is **"OL
  Engineering (ARCHIVED)"**. The consumer map labels it "ol-eng-library" under
  `ol-eng-library-platform@` and "Pulumi.QA" under `ocw-studio-rc@`
  (`gcp-service-account-consumer-map.md:64`, `:68`). The names were wrong but the
  memberships were right: `ocw-studio-production@`, `ocw-studio-rc@` and
  `ol-eng-library-platform@` (Heroku run, 2026-09-17) are **all organizers** on
  it, and the drive is archived.
- **A production credential depends on a personal Gmail account.** The xPro
  enrollments folder is owned by personal Gmail A. `xpro-coupon-requests-productio@`
  holds content-manager/writer on it by that person's grant. If that account lapses
  or is cleaned up, the grant goes with it — and re-issuing after consolidation
  requires that account's owner personally. This is a live single point of failure, not just a
  migration cost.
- **Two genuinely external BigQuery grants**, neither visible to any OL gcloud
  identity: `mitx-residential-pipeline-main` and `mitir-mitx-surveys` (MIT
  Institutional Research). Re-issuing these needs their owners.

### The "edx.org grants" were mis-stated, and the real consumer was missing

Resolved 2026-08-24, and it changes who the counterparty is.

**The consumer map's attribution was internally inconsistent.** It credited
`ol-data-platform-qa@`'s BigQuery traffic to the "Dagster `edxorg` /
`legacy_openedx` code locations (Vault `secret-data/pipelines/edx/org/gcp-oauth-client`
→ `GCSConnection`)". A `GCSConnection` makes no BigQuery calls. Searching
`ol-data-platform/dg_projects` finds **no BigQuery client anywhere** — the only
Google client in those code locations is `storage_client.list_blobs`.

**The BigQuery consumer is Airbyte** (confirmed by the project owner). So this
credential has *two* distinct consumers, and only one was recorded:

| Consumer | API | What it reaches |
|---|---|---|
| Dagster `edxorg` | GCS | bucket `simeon-mitx-pipeline-main`, prefix `COLD/` |
| Airbyte | BigQuery | project-level access; datasets per the probe |

**All three BigQuery projects are IRx's** (MIT Institutional Research), confirmed
by the project owner 2026-08-25: `mitx-pipeline-main-dc29`,
`mitx-residential-pipeline-main` and `mitir-mitx-surveys`. That collapses what
looked like three unknown counterparties into **one, and an internal MIT one**.
The tool still marks them third party, which is correct in its own terms — they
sit outside OL's project hierarchy, so grants naming a replacement SA must be
re-issued — but "third party" should be read as "another MIT department we can
ask", not "an external organisation with no relationship to us".

Corroborated in-repo: `lakehouse/definitions.py:243-244` declares Airbyte asset
groups `irx_bigquery__s3_data_lake` and `irx_bigquery_email_opt_in__s3_data_lake`
on a 24-hour production interval — IRx BigQuery syncing into the S3 data lake,
which is exactly the traffic measured.

**The GCS bucket is not edx.org's.** `edxorg_archive.py:74` and `:539` name it
`simeon-mitx-pipeline-main` — the MITx "simeon" pipeline, matching BigQuery project
`mitx-pipeline-main-dc29`. The *data* originates at edx.org: the asset description
reads "Archive of data exported from edx.org for courses run by MIT. Generated by
https://github.com/openedx/edx-analytics-exporter/". The register had been
conflating the data's **origin** with the grant's **counterparty**. Re-issuing
after consolidation means asking whoever runs the simeon pipeline, not edx.org.

**Traffic re-verified live, 30 days to 2026-08-24** (`bin/gcp-credential-usage
report --days 30 ol-data-platform`), which also corrects the map's figure:

```
50,631  bigquery  TableDataService.List   ol-data-platform-qa@
 2,808  bigquery  TableService.GetTable   ol-data-platform-qa@
   141  bigquery  TableService.ListTables ol-data-platform-qa@
   136  bigquery  JobService.InsertJob    ol-data-platform-qa@
 2,706  sheets    GetSpreadsheet          ol-data-platform-production@
```

(The two `ProjectService.ListProjects` rows in that report are this tool's own
probe calls — worth filtering out of any future reading, as the consumer map
already notes for the gcloud CLI's public OAuth client.)

**A credential that turned out not to exist.** `dagster_server_policy.hcl` granted
read on `secret-operations/institutional-research-bigquery-service-account`, which
looked like an uninventoried credential for `mitir-mitx-surveys`. Checked
2026-09-17: **the secret does not exist in either the QA or the production Vault
cluster.** Its only consumer was the `mitx_bigquery` pipeline template, removed in
`a6a5f1739` ("Removing defunct data pipelines", 2022-07-11); the policy grant was
left behind and is now removed. No service account with an IR or BigQuery name
exists in any project an OL identity can list, other than
`bigquery-redash@mitx-residential`, already slated for retirement in the consumer map.

So there is no institutional-research credential to re-home, and no IR grant to
re-issue for it. The IRx BigQuery grants that do exist are held by
`ol-data-platform-production@` and `-qa@` (Airbyte), and are re-issued with those.
An earlier version of this section treated the credential as live and OL-owned and
planned a lead-time grant request around it; that plan is withdrawn.

### An entire credential family that no inventory pass covered

`src/bridge/secrets/airbyte/data.production.yaml` holds **three Google service
accounts**, none of which appears in the consumer map, the credential inventory,
or any earlier triage:

| SOPS field | Consumer |
|---|---|
| `google_service_account_json` | Airbyte's own source credential |
| `emeritus_google_service_account_json` | Emeritus BigQuery → S3 |
| `global_alumni_google_service_account_json` | Global Alumni BigQuery → S3 |

They were missed because **every prior pass looked in Vault**, and these are in
SOPS. `data.qa.yaml` carries only `google_service_account_json`; `data.ci.yaml`
should be checked too.

Two of them matter more than the count suggests. **Emeritus and Global Alumni are
external commercial partners, not MIT departments.** If those service accounts were
issued from the partners' own GCP projects, they are credentials OL genuinely
cannot re-create — the situation wrongly attributed to the IRx credential earlier
in this document, now looked for in the right place. Their traffic is also
invisible to every GCP-side signal we have, because `bin/gcp-credential-usage`
reads per-project monitoring and those projects are not ours.

Probing them settles it: `client_email` names the owning project. All three are now
in `probe-all`'s manifest, reached through a new `--sops FILE:FIELD` source.

#### SOPS run, 2026-09-17

| SOPS file : field | Identity | Home project | Result |
|---|---|---|---|
| `data.production.yaml:google_service_account_json` | `ol-data-platform-production@` | `ol-data-platform` (OL) | Same 3 IRx BigQuery projects as the Vault copy |
| `data.qa.yaml:google_service_account_json` | `ol-data-platform-qa@` | `ol-data-platform` (OL) | Same identity as the QA Vault copy (not re-probed) |
| `data.ci.yaml:google_service_account_json` | `ol-data-platform-ci@` | `ol-data-platform` (OL) | **Token exchange `invalid_grant`** |
| `data.production.yaml:emeritus_google_service_account_json` | `mit-xpro-sis@` | **`emeritus-data-science`** (Emeritus) | **Token exchange `invalid_grant`** |
| `data.production.yaml:global_alumni_google_service_account_json` | `mit-xpro-bigquery@` | **`global-alumni-365215`** (Global Alumni) | BigQuery on its own project: datasets `mit_xpro_api`, `_9ec9b669…` |

What this settles:

- **Both partner credentials were issued from the partners' own projects.** OL
  cannot re-create them, and does not need to: they are not in OL's estate, so
  consolidating into `mitol01` does not touch them. They drop out of the
  migration's re-issue list. What they do need is an owner on the OL side who
  knows to ask the partner when a key has to be rotated.
- **The Emeritus key in SOPS is dead, but the sync is not.** `invalid_grant` on a
  well-formed key means the key was deleted or disabled in `emeritus-data-science`.
  The SOPS copy dates from 2024-11-21 (`7c080e607`). Yet
  `s3://ol-data-lake-raw-production/raw__emeritus__bigquery__api_enrollments/data/`
  took a 3.5 MB file on 2026-09-16, so production Airbyte holds a newer, working
  key that was entered by hand and never written back to SOPS. SOPS is not the
  source of truth for this credential; the Airbyte connection config is.
- **The CI Airbyte secret is a hand-edited copy of QA's.** CI and QA carry the
  same `private_key_id` but different `client_email`s. A key id belongs to exactly
  one SA, so the CI secret pairs QA's private key with the CI SA's address, and
  Google rejects the assertion. Replacing the CI secret with a real
  `ol-data-platform-ci@` key (or with the QA one, unedited) is the fix. The
  `invalid_grant` is confirmed; the edit as its cause is inferred from the key id.
- **Airbyte's production credential is `ol-data-platform-production@`**, the same
  identity as Vault `secret-data/pipelines/google-service-account`. The 30-day
  traffic above shows the IRx BigQuery reads under `ol-data-platform-qa@`, not
  `-production@`. Either QA Airbyte runs the IRx syncs, or production Airbyte's
  connection was configured by hand with the QA key rather than from SOPS.
  **Unresolved**, and it matters for cutover: the replacement SA has to go into
  whichever Airbyte actually runs the sync.

A tool fix forced by this run: a partner-issued key was vouching for its home
project, so `global-alumni-365215` reported as OL's (`third_party: false`). The
same rule would have added a partner's org folder to the owned-parent set, and
laundered everything else in that org. Manifest entries marked `partner` now
never vouch, and their home projects classify as third party.

### Dataset-level enumeration

Project-level enumeration cannot answer "what were we actually granted". A
credential appears against a project whether it holds one dataset there or all of
them, and for a third party's project that difference is the whole negotiation:
re-issuing one dataset grant is a different conversation from re-issuing
project-wide access. The BigQuery probe now lists datasets per project
(`datasets?all=true`), reported in both output formats.

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
- **`ol-eng-library-platform@` produced no rows**: the Heroku step needs the
  `heroku` CLI logged in. Covered by the Heroku run, 2026-09-17.
- **No Analytics or YouTube grants appeared for any credential.** Explained on
  2026-09-17: see "Heroku run" below. The 403s are the API being disabled in the
  credential's own project, not a refused scope.

### Heroku run, 2026-09-17

`ol-eng-library`, `mit-open-library` and `mit-open-library-rc` all carry the same
`ol-eng-library-platform@engineering-project-management` key (checked by the
project owner), so one probe covers all three apps. Rows are in the Findings table.

- **It is organizer on three Shared Drives, not two.** The third is `0ADY3FaGtq2jvUk9PVA`
  "Open Learning Engineering", where `xpro-coupon-requests-testing@` is also a
  member. `0AAx-gQtx1vVKUk9PVA`, labelled "mit-open-library" in the consumer map,
  is named "MIT Open". Re-homing means adding the replacement SA to all three
  drives. Whether the library apps need organizer rather than a lower role was not
  checked.
- **A non-MIT organisation shared into it.** Two "StakeholderSession*Feedback"
  folders are shared by gostudion.com vendor account. Reader only, and nothing shows
  the apps depend on them, but they would need that person to re-share.
- **The same two slide decks are shared with `xpro-coupon-requests-testing@`.** The
  owners shared "Compliance and Refusal Tech Talk" and "Frontend tooling" with
  more than one service account, so these are not one-off typos.

#### What the 403s mean, and the gap they expose

Every Analytics and YouTube 403 from today's runs reads "API has not been used in
project N before or it is disabled". That is the credential's **own** project
refusing the call, not a grant lookup coming back empty. A service account's calls
bill to its own project unless the caller names another, so these credentials
cannot use Analytics or YouTube as deployed, and hold no grant there worth
re-issuing.

The same message came back for **Drive** on `ol-data-platform-production@` and
`ol-data-platform-qa@`, because `drive.googleapis.com` was disabled in
`ol-data-platform`. The Drive probes had never enumerated those two credentials.

### Drive re-run for ol-data-platform, 2026-09-17

`drive.googleapis.com` was enabled in `ol-data-platform` on 2026-09-17 (as
the legacy devops Gmail account, project owner; the project is not in the Pulumi GCP stack,
so this is not drift), and both credentials were re-probed through their SOPS copies.
Rows are in the Findings table.

**The Canvas sheet does not appear in either listing, yet both credentials can read
it.** `13AoothEh…` ("Canvas GenAI - Pilot Tracker", the sheet
`dg_projects/canvas/canvas/definitions.py:70` reads) lives in Shared Drive
`0ADY3FaGtq2jvUk9PVA` "Open Learning Engineering". Neither credential is a member
of that drive, and the file is not in `sharedWithMe`. A direct `files.get` as each
credential succeeds with read-only capabilities. So the access arrives by a route the
listings do not show: link sharing ("anyone with the link") or a group the service
account belongs to. Which one matters for cutover: link sharing carries over to a
replacement SA with no action, a group needs the new SA added. **Not determined**;
the sheet's sharing settings have to be read by someone with edit access to it.

This is a blind spot in the tool, not in this one run: **a listing can only find
grants that name the credential.** Any credential's link-shared or group-mediated
access is invisible to `probe-all`, and is only found by asking for a known file id.
Added to "What still needs a human".

Other results:

- **`MITx [Entitlements by program]`** (`1Bd5igss…`) is shared with
  `ol-data-platform-production@` as writer by MIT staff account F. It is not referenced
  in `ol-data-platform`, `ol-infrastructure` or `mitxonline`. It is a candidate for
  the unidentified Sheets calls on this credential; **unconfirmed**, and the consumer
  could be a hand-configured Airbyte Google Sheets source.
- **`Copy of Video Content Calendar`** is shared with `ol-data-platform-qa@` as
  writer by personal Gmail E.
- **`MIT Moments Content Calendar`** sits in Shared Drive `0AP-f7PkqZpuxUk9PVA`,
  which neither credential is a member of, so it reaches them by a direct share
  inside that drive. The drive's name is not visible to them.
- **The QA credential's canvas test sheet** (`18YaTPPz…`, "Test canvas course IDs
  ingestion") is shared by MIT staff account F, matching the production sheet's owner.

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
| "Compliance and Refusal Tech Talk" (presentation) | reader | personal Gmail C |
| "Frontend tooling" (presentation) | reader | personal Gmail D |
| "Sheets API Testing" (folder) | content-manager/writer | personal Gmail B |

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
  As an OL engineer's MIT account that returns only `mitol-engineering` and `mitol01`, so the
  entire legacy estate classified as external.
- The obvious repair — union across all credentialed accounts — reclassified
  `mitx-pipeline-main-dc29` as OL-owned. **Also wrong, and worse.**
  `gcloud projects list` returns every project an identity can *see*, which
  includes third-party projects OL has been **granted into**. Those grants are the
  population this tool exists to find, so keying ownership on visibility launders
  them into "internal" and drops them from the migration plan. The grant shows up
  as proof of ownership.
- Confirmed directly: `mitx-pipeline-main-dc29` sits in `folder/249626760288`,
  which the legacy devops Gmail account cannot even describe. It appears in `projects list`
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

`0AErNBMZMmOz3Uk9PVA` was recorded against both `ol-eng-library-platform@` and
`ocw-studio-rc@` and looked like a collision. The probes showed both are members;
see "What this run settled".

## What still needs a human

The probes do not cover these, and no amount of tooling will make them cover them.

1. **Generic OAuth 2.0 clients.** No create, update or even *list* API exists. The
   three live ones are known only because they showed traffic; dormant ones cannot be
   inventoried at all. Their grants must be walked in the Console, project by project.
2. **Google Ads links.** Not reachable with a read-only service-account token.
3. **YouTube channel permissions.** `channels.list?mine=true` answers for an OAuth
   client, not for the human-managed channel permissions behind OCW Studio publishing.
4. **Grants made to a Google *group* the credential belongs to, and link sharing.**
   This entry used to say the probe sees such access but not its route. The
   2026-09-17 re-run showed it can miss the access entirely: the Canvas sheet is
   readable by both `ol-data-platform-*` credentials and appears in neither the
   Shared Drive nor the `sharedWithMe` listing. Access like this is found only by
   `files.get` on a known id. The resource's sharing settings then say whether a
   replacement SA inherits it (link sharing) or has to be added to a group.
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
