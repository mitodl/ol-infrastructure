# GCP external-grant register

Status: method established and tooled; per-credential enumeration not yet run.
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
otherwise discoverable only by asking them — shows up directly. Third-party
classification is derived at runtime from `gcloud projects list` rather than a
hardcoded name list, because mislabelling an OL-owned project as third-party
invents a negotiation that does not need to happen.

### Verification performed, 2026-08-24

The probe path (token acquisition → HTTP → classification → report) was exercised
end-to-end against two live gcloud identities. `bigquery.projects.list` returned 13
projects for `mitx.devops@gmail.com`, all correctly classified as OL-owned. The
Drive/Analytics/YouTube probes returned `403 insufficient authentication scopes`,
which is the expected and correct result for a gcloud-minted token: gcloud issues
`cloud-platform` scope only.

Credential loading and grant classification are covered by
`tests/bin/test_gcp_external_grants.py` (11 tests), which pins all three secret
shapes, the escaped-PEM case, and the ownership rules below.

**The service-account path has not yet been exercised against a real key.** Until
one of the runs below is done, treat the Drive/Analytics/YouTube probes as designed
and tested but unproven against live Google APIs.

### One classification rule worth knowing before reading output

A service-account address cannot be judged by its domain. **Every** project's
service accounts live under `*.iam.gserviceaccount.com` — a third party's as much
as ours — so the project id embedded in the address is checked against
`gcloud projects list` instead. Where ownership cannot be determined (gcloud
unavailable, or an address with no domain) the tool reports *unknown* rather than
guessing. Guessing "OL-owned" would silently drop a third-party grant from the
migration plan, which is the expensive direction of that error.

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

*(empty — populate from probe output)*

| Credential | Product | Resource id | Resource name | Access | Grantor | Third party? |
|---|---|---|---|---|---|---|

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
