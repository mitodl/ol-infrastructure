# GCP → Pulumi import strategy, per resource type

Status: spec. Supersedes nothing; this is the first design pass.
Evidence base: `docs/plans/gcp-service-account-consumer-map.md` (discovery,
2026-08-10). Scaffolding this design targets: `src/ol_infrastructure/infrastructure/gcp/`,
`src/ol_infrastructure/components/gcp/project.py`, `src/ol_infrastructure/lib/gcp/provider.py`.

## What this document decides

How each kind of live GCP resource comes under Pulumi management **without
being destroyed and recreated**. For a service account or an API key,
"recreated" means the credential value changes, which means every consumer
holding the old value breaks at the moment of the change — and the consumer map
shows that several consumers are external to GCP entirely (Heroku apps, Google
Sheets, a Shared Drive, an edx-platform setting) and would not fail visibly in
any GCP-side signal.

It does **not** decide the target organization layout, project naming, or which
projects get consolidated. Those are blocked on IS&T
(`tk-establish-with-is-amp-t-what-ol-can-do-above-pro-bf8bdd`) and are
deliberately independent: adoption happens in the projects that exist today,
and a project that is later consolidated is a *second* migration of an
already-managed resource rather than a first migration of an unmanaged one.

## The manageability matrix

Four credential types exist in the estate. They are not equally manageable, and
a plan that assumes they are will silently skip the highest-traffic ones —
four of the six highest-traffic credentials are not service accounts.

| Type | Pulumi resource | Importable | Rotation blast radius |
|---|---|---|---|
| Enabled service (API) | `gcp.projects.Service` | yes | none — enabling is idempotent |
| Service account | `gcp.serviceaccount.Account` | yes | replacement changes the email; every grant and consumer breaks |
| Project IAM grant | `gcp.projects.IAMMember` | yes | non-authoritative, so low |
| API key | `gcp.projects.ApiKey` | yes | replacement changes the key string; consumers break silently |
| reCAPTCHA key | `gcp.recaptcha.EnterpriseKey` | yes | site key is compiled into deployed frontends — rotation is an app release |
| Generic OAuth 2.0 client | **none** | **no** | not creatable, updatable, *or listable* by any API |
| Service-account key (JSON) | `gcp.serviceaccount.Key` | technically | **not to be managed** — see below |

Two entries need their reasoning stated rather than assumed:

**Generic OAuth clients have no API at all.** Not merely no Pulumi resource —
no create, no update, no list. They stay hand-created, with the client id and
secret stored in Vault/SOPS and referenced from application config. Because
they cannot be listed, dormant ones cannot be inventoried; the only ones we
know about are the three that showed traffic. Any plan that says "enumerate the
OAuth clients" is describing something Google does not offer.

**Service-account keys are managed by not existing.** `gcp.serviceaccount.Key`
would put private key material into Pulumi state. The point of this project is
to remove long-lived key material, so the component does not expose the
resource. Workloads authenticate via Workload Identity Federation; the
exceptions that genuinely cannot are hand-created and recorded as exceptions.

## Import identifiers, per type

Verified against the upstream `terraform-provider-google` documentation on
2026-08-19 (the Pulumi Python SDK ships empty docstrings for these resources,
so the provider's own docs are the source).

| Resource | Import id format | How to get the id |
|---|---|---|
| `gcp.projects.Service` | `{{project_id}}/{{service}}` | `gcloud services list --enabled --project=P --format='value(config.name)'` |
| `gcp.serviceaccount.Account` | `projects/{{project_id}}/serviceAccounts/{{email}}` | `gcloud iam service-accounts list --project=P --format='value(email)'` |
| `gcp.projects.IAMMember` | `"{{project_id}} {{role}} {{member}}"` — **space-delimited** | `gcloud projects get-iam-policy P --format=json` |
| `gcp.projects.ApiKey` | `projects/{{project}}/locations/global/keys/{{name}}` | `gcloud services api-keys list --project=P --format=json` — take `name` |
| `gcp.recaptcha.EnterpriseKey` | `projects/{{project}}/keys/{{name}}` | `gcloud recaptcha keys list --project=P` |

Two traps in that table:

- The IAM member id is **space**-delimited, not slash-delimited like every
  other GCP import id. `"my-project roles/viewer user:foo@example.com"`.
- The API key id ends with the key's **`name`** — the last segment of its API
  resource name, an RFC-1034 string matching
  `[a-z]([a-z0-9-]{0,61}[a-z0-9])?`. Not its display name, and **not its
  `uid`**, which is a separate output-only UUID4 field on the same resource.
  The provider's own attribute reference settles it: `id` is
  `projects/{{project}}/locations/global/keys/{{name}}`. Read `name` off
  `--format=json` rather than assembling the id from parts, and note
  `{{project}}` in what Google returns is the project *number* — both the id
  and number forms are accepted.

## The mechanism: `import_`, not `pulumi import`

Pulumi offers two adoption paths. This project uses the declarative one.

`pulumi import <type> <name> <id>` writes directly to state and prints
suggested code. It is an out-of-band mutation: nothing in the repository
records that it happened, and a reviewer reading the PR cannot tell an adopted
resource from a newly created one.

`ResourceOptions(import_=<id>)` is declared in code, reviewed in the PR,
adopted on the next `up`, and leaves a durable statement in config of which
resources predate Pulumi. `OLGCPProject` takes an `import_id` on each service
account and API key and turns it into exactly this, plus `protect=True` — an
adopted resource has consumers the stack does not know about, so any diff that
resolves to a replacement must fail loudly rather than proceed.

The cost of the declarative path is that the declared inputs must match the
live resource; Pulumi refuses the import otherwise. That is a feature at this
scale (16 service accounts, 14 API keys, 16 reCAPTCHA keys) — the mismatch
message names the properties, and converging on them is how you learn what the
live resource actually is. Use a throwaway `pulumi import ... --out` run purely
as a *read* when a property's live value is unclear; do not keep its state.

### The unrestricted-key ordering constraint

This falls out of the mechanism and is easy to get backwards. Six API keys
carry no restriction of any kind, and `OLGCPAPIKeyConfig` refuses to declare an
unrestricted key. But `import_` requires the declared inputs to match the live
resource — so an unrestricted key cannot be imported *as* a restricted one in a
single step.

Therefore: **restrict (or delete) the six unrestricted keys before importing
any of them.** `tk-restrict-or-delete-11-idle-and-6-unrestricted-gc-bd93d3` is
not the cleanup task it looks like; it is a hard prerequisite of API key
adoption. The same applies to `mitx-residential`'s 2015 "Server key 1", whose
repointing task (`tk-repoint-edxapp-mitx-residential-off-mitx-residen-54381e`)
must land before that key is either restricted or adopted.

## Safety rules that come out of discovery

These are not general Pulumi advice. Each one is a specific reading of what the
consumer map found.

1. **Never disable a service on destroy.** `disable_on_destroy=False` is set
   explicitly on every `gcp.projects.Service`, not left to the provider
   default. Removing a service from the stack must not turn the API off for a
   consumer the stack does not know about — and several consumers emit no
   GCP-side usage signal at all. reCAPTCHA classic verification goes to
   `www.google.com/recaptcha/` and produces no `request_count`, no last-auth,
   no metric of any kind; GCS reads do not report caller-side `request_count`
   either. Zero traffic is never proof of death.

2. **Never use `gcp.projects.IAMPolicy` or `gcp.projects.IAMBinding` on a
   legacy project.** Both are authoritative: they delete grants they do not
   declare. Legacy projects carry direct Owner grants held by personal Gmail
   accounts, and while removing those is a goal
   (`tk-remove-personal-gmail-accounts-holding-direct-ow-e988ba`), removing
   them as a *side effect* of an unrelated import would lock out the only
   identity that can currently see some of these projects. `IAMMember` is
   non-authoritative and is the only IAM resource this component uses.

3. **Adopt only the services in use, not everything enabled.** A GCP project
   typically has 20–40 services enabled, most of them Google defaults nobody
   chose. Declaring all of them buys nothing and makes every future diff
   noisier. Adopt the services the consumer map proves are called, plus their
   declared dependencies; leave the rest unmanaged. Rule 1 means nothing gets
   turned off by that choice.

4. **`protect=True` travels with every import.** Dropped deliberately, per
   resource, once that resource's consumers are known — never in bulk.

5. **Do not adopt anything in a project scheduled for consolidation until the
   consolidation target is known.** Adopting resources in a project that is
   about to be replaced doubles the migration work. The exception is
   remediation that must happen regardless: restricting an unrestricted key,
   deleting a dead credential.

6. **Nothing in `recaptcha-migrated-075600d5919` or the three `mit-bounty` keys
   in `ocw-studio-qa` is adopted.** The former is a Google-generated
   auto-migration project whose disposition is undecided; the latter belongs to
   someone else and is to be handed back
   (owner-confirmed, 2026-08-10), not migrated.

## Adoption order

Ordered by risk, lowest first. Each step is a separate PR and a separate
`pulumi up`.

1. **Enabled services.** No credential value, no consumer breakage, and
   `disable_on_destroy=False` makes the operation one-directional. This is the
   step that proves the provider, credential path, and stack layout work.
2. **Service accounts with no external grants.** The account exists; its email
   does not change under an in-place update. Import the account, then its
   project IAM members.
3. **Service accounts with external grants** — the four holding Shared Drive
   and Sheet grants. Same mechanics, but a replacement here breaks a share
   living in a different Google product that no GCP API can see. These stay
   protected indefinitely, and
   `tk-enumerate-external-google-product-grants-per-cre-070a83` should complete
   first so the blast radius is written down rather than guessed.
4. **API keys**, after the restrict-or-delete pass. Highest silent-failure risk
   in the estate: a replaced key string produces a 403 in the consumer, not an
   error in GCP.
5. **reCAPTCHA keys**, last, and only alongside an application release —
   the site key is deployed inside frontends.

## Provider credentials

`ol_infrastructure.lib.gcp.provider.gcp_provider()` is the only way a stack
gets a provider, and the project id is always explicit. The provider's own
fallback is the gcloud config value, which is whatever the operator last ran
`gcloud config set project` with, and the ambient-credential fallback is
whichever Google account the machine happens to be logged in to. Inheriting
that is how this estate came to be owned by personal Gmail accounts; the
inventory work itself required switching `gcloud config set account` between
identities to see different halves of it.

Two credential shapes are accepted, distinguished by the `type` field in the
credential JSON:

- `external_account` — Workload Identity Federation, exchanging the Concourse
  worker's AWS IAM role for a short-lived Google token. Target state.
- `service_account` — a downloaded key. Accepted only for bootstrapping,
  because standing up federation requires an identity that predates it. Every
  use logs a warning naming the stack.

Federation cannot be configured until there is a project OL controls in which
to create the workload identity pool, so the bootstrap path is the real one for
now. This is the single largest open dependency in the design, and it is the
same IS&T conversation.

## Open questions

- **Where do OL's projects live?** Org/folder parent, billing account, and
  whether OL can create projects itself. Blocks project creation, workload
  identity pool creation, and any org-level policy. →
  `tk-establish-with-is-amp-t-what-ol-can-do-above-pro-bf8bdd`
- **What external grants exist per credential?** No GCP API can answer this;
  it is a per-product walk through each Google product's own admin UI. The
  largest hidden cost in the migration. →
  `tk-enumerate-external-google-product-grants-per-cre-070a83`
- **Which GCP project owns learn-ai's `GEMINI_API_KEY`?** Found while writing
  this design, and not in the credential inventory. It is a live secret in
  `src/bridge/secrets/learn_ai/secrets.{ci,qa,production}.yaml`, written to
  Vault by the learn-ai stack, and a Gemini/AI-Studio key is a GCP API key
  backed by *some* GCP project. The inventory enumerated the projects visible
  to `mitx.devops@gmail.com`, so a key created under a different account is
  outside everything measured so far. There is also a `GEMINI_API`
  (`ol-infrastructure-gemini-api`) project-name constant in
  `lib/pulumi_projects.py` with no stack directory behind it, which suggests
  someone already intended to manage this and stopped. The estate is therefore
  larger than the inventory says, in a direction the inventory's method could
  not see.
- **Do we generate the stack config, or hand-write it?** The Sentry stack
  (`infrastructure/sentry/`, generated by `bin/import-sentry-config` with an
  `IMPORT_SUMMARY.md` recording every provider caveat and hand-authored
  exception) is the precedent, and the estate is a similar size. A generator
  pays for itself if the config is re-derived from live state more than once.
  Deferred until the first project is adopted by hand and the shape of the
  config is settled by experience rather than by guess.
