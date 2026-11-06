# GCP infrastructure stacks

Pulumi management of Google Cloud Platform projects, credentials and enabled
APIs. This is the landing point for the work tracked in
`docs/plans/gcp-consolidation-into-mitol01.md`, which is where the *why* of every
convention below is argued out. The credential inventory those decisions rest
on is `docs/plans/gcp-service-account-consumer-map.md`.

## Where new credentials go

New Google credentials of any type (service account, API key, OAuth client,
reCAPTCHA key) go in `mitol01`. Do not create them in the legacy projects
(`mit-open`, `ocw-studio-qa`, `mitx-residential` and the rest of the inventory
in `docs/plans/gcp-service-account-consumer-map.md`), even when the existing key
for that application lives there.

Service accounts, API keys and reCAPTCHA keys cannot be moved between
projects, so each one created in a legacy project has to be re-created in
`mitol01` later, with a new email or key string, its external grants re-issued
by hand and a deploy of the application that holds it. The reCAPTCHA keys for
learn.mit.edu and rc.learn.mit.edu were created in `mit-open` in April 2026 and
are now two more keys to re-create.

Service accounts, API keys and reCAPTCHA keys are declared in
`Pulumi.Production.yaml` (see [Configuration](#configuration) and
[reCAPTCHA keys](#recaptcha-keys)). OAuth clients are created by hand in
`mitol01` (see [What this does not manage](#what-this-does-not-manage)). If `mitol01` is
missing an API you need, add it to `enabled_services`. A gap in `mitol01` is
something to fix here, not a reason to use a legacy project.

YouTube quota is the one gap config cannot close. `mitol01` starts at the default
10,000 units a day, and a granted increase does not follow a consumer to
another project, so a high-volume YouTube consumer needs the increase granted
on `mitol01` first (see `docs/plans/gcp-consolidation-into-mitol01.md`).

### Watching the legacy projects for new credentials

The rule above has no enforcement. We hold no organization-level control over
the legacy projects (they have no parent organization), so the backstop is
detection: `bin/gcp-credential-usage diff` lists service accounts,
user-managed service-account keys, API keys and reCAPTCHA keys in each project
and reports any that are missing from `legacy-credential-baseline.json` in this
directory. It exits 1 when it finds one, and 2 when a project could not be
read.

```bash
bin/gcp-credential-usage diff                      # projects in the baseline
bin/gcp-credential-usage diff --all-projects       # also catch a new project
bin/gcp-credential-usage diff --since 2026-08-10   # also flag baselined keys
```

Generic OAuth clients are not covered, because Google has no API that lists
them. reCAPTCHA keys are only listed where `recaptchaenterprise.googleapis.com`
is already enabled (four projects today); the command never enables an API, and
reports the rest as not checked. A service account has no creation time in the
IAM API, so a new one shows up only as missing from the baseline.

`--all-projects` is for manual runs. It checks every project the account can
list, so a project it cannot read (e.g. `mitx-pipeline-main-dc29`, below) or one
that was never meant to be in the baseline fails every run. A scheduled run
should check the baseline's projects, which is the default.

Once a finding is understood (moved, deleted, or accepted as a known exception),
regenerate the baseline and commit it:

```bash
bin/gcp-credential-usage snapshot $(jq -r '.projects[]' \
  src/ol_infrastructure/infrastructure/gcp/legacy-credential-baseline.json) \
  --output src/ol_infrastructure/infrastructure/gcp/legacy-credential-baseline.json
```

`snapshot` refuses to write a baseline if any read fails, because a partial
baseline would turn every credential it missed into a false "new" finding.
The baseline holds no key material. API keys are recorded by uid,
service-account keys by key id, and reCAPTCHA keys by a digest of the resource
name, which ends in the site key. `mitx-pipeline-main-dc29` is listable by the
account that owns the other legacy projects but not readable by it, so it is
not in the baseline.

#### Running it on a schedule

This is not wired into Concourse yet, because no identity Concourse can assume
can read the legacy projects. The production `infra` worker federates to
`pulumi-gcp@mitol01`, and no `mitol01` identity holds any role on the legacy
projects (checked against all 19 project IAM policies on 2026-10-19). Those
projects have no organization, so only their owner can grant access, and
nothing in this stack can do it.

Wiring it up needs:

1. A read-only service account in `mitol01`, e.g. `gcp-credential-watch`,
   declared in `Pulumi.Production.yaml` with the same
   `roles/iam.workloadIdentityUser` binding that `pulumi-gcp` has. Granting the
   legacy read to `pulumi-gcp` would also work with no new resources, but every
   GCP Pulumi run would then carry it, and removing it later would mean
   editing the deploy identity.
2. The owner of the legacy projects granting that account
   `roles/iam.securityReviewer` on each project in the baseline:

   ```bash
   for project in $(jq -r '.projects[]' legacy-credential-baseline.json); do
     gcloud projects add-iam-policy-binding "$project" \
       --member=serviceAccount:gcp-credential-watch@mitol01.iam.gserviceaccount.com \
       --role=roles/iam.securityReviewer --condition=None
   done
   ```

   `securityReviewer` holds every permission the command uses
   (`iam.serviceAccounts.list`, `iam.serviceAccountKeys.list`,
   `apikeys.keys.list`, `recaptchaenterprise.keys.list`,
   `serviceusage.services.list`). Do not substitute `roles/viewer`: it also
   holds `apikeys.keys.getKeyString`, which reads every API key string in the
   project.
3. A token in the task. The command takes its token from
   `gcloud auth print-access-token`, and the `mitodl/ol-infrastructure` image
   has neither `gcloud` nor `google-auth`. `gcloud auth login --cred-file`
   accepts an `external_account` document like the one in
   `src/bridge/secrets/gcp/credentials.yaml`, pointed at the new account's
   impersonation URL, so the task needs the Google Cloud CLI available.

With those in place the pipeline has the same shape as
`src/ol_concourse/pipelines/infrastructure/alerting_drift/`: run `diff` nightly,
write its text output to a report, and open or update a pull request when the
report changes. The text output contains no timestamps from the run itself, so
an unchanged estate produces an unchanged report.

## Stack layout

One stack, `Production`.

```bash
cd src/ol_infrastructure/infrastructure/gcp/
pulumi stack select Production
pulumi preview
```

`mitol01` is the consolidation target for the whole estate, so an
environment-tiered layout would put three stacks on one GCP project. That buys
naming, not isolation: every role the automation account needs
(`apiKeysAdmin`, `serviceAccountAdmin`, `serviceUsageAdmin`) is project-scoped,
so a `pulumi-gcp-ci@` holding `apiKeysAdmin` on `mitol01` can modify the
production key regardless of which stack declares it.

It also costs. Anything project-scoped — enabled services, org policy, project
metadata — exists once per project, so each would need a "declare this in
Production only" carve-out to stop two stacks fighting over one resource. One
such rule is a footnote; one per resource type is a design.

The tier a credential serves is carried in its own name and restrictions —
`learn-ai-qa` against `learn-ai-production` — which one stack expresses without
ceremony.

If a second stack is ever warranted, split it by *blast radius* over a
different set of GCP projects, not by tier over the same one.

## Bootstrap: the APIs the provider itself needs

`enabled_services` holds two different kinds of thing, and the difference
matters when standing a project up.

Most entries are APIs an *application* consumes — `youtube.googleapis.com`,
`generativelanguage.googleapis.com`. Pulumi can enable those from a standing
start.

But the provider also needs APIs in order to **read** the resource types this
stack manages: `iam.googleapis.com` to see service accounts,
`apikeys.googleapis.com` to see API keys,
`recaptchaenterprise.googleapis.com` to see reCAPTCHA keys,
`serviceusage.googleapis.com` to
see which services are enabled (every `gcp.projects.Service` lists them before
creating one), and `cloudresourcemanager.googleapis.com` to read and write the
project IAM policy for `project_roles`. Those have to be enabled *before* the
first apply, because a stack cannot enable the API it needs in order to look
at itself.

`pulumi preview` does not catch a missing one. A resource that is not yet in
state is planned as a create without being read, so the 403 only appears
during `pulumi up`. The first production deploy failed this way on
`serviceusage.googleapis.com`.

So the bootstrap is one out-of-band command per project:

```bash
gcloud services enable iam.googleapis.com apikeys.googleapis.com \
  recaptchaenterprise.googleapis.com serviceusage.googleapis.com \
  cloudresourcemanager.googleapis.com --project=<project>
```

and then the same services are declared in `enabled_services` so they stay
enabled and the fact is recorded. Add to that list whenever a new managed
resource type brings its own API.

The automation account's own project roles are bootstrap too. They are granted
by a project Owner, never by the stack, so the account cannot widen its own
access through a config change. `pulumi-gcp@mitol01` needs
`iam.serviceAccountAdmin`, `resourcemanager.projectIamAdmin`,
`serviceusage.apiKeysAdmin` and `serviceusage.serviceUsageAdmin` (granted
before the stack existed), plus `iam.workloadIdentityPoolAdmin` once a project
declares `workload_identity_pools` and `recaptchaenterprise.admin` once it
declares `recaptcha_keys`:

```bash
for role in roles/iam.workloadIdentityPoolAdmin roles/recaptchaenterprise.admin; do
  gcloud projects add-iam-policy-binding mitol01 \
    --member=serviceAccount:pulumi-gcp@mitol01.iam.gserviceaccount.com \
    --role="$role" --condition=None
done
```

Note this is invisible from `gcloud`: `gcloud services api-keys list` succeeds
against a project where `apikeys.googleapis.com` is not enabled, because the
CLI bills the call elsewhere. Only the provider, which scopes the quota project
to the target, sees the difference.

`compute.googleapis.com` is deliberately **not** enabled. The provider logs
`failed to get regions list` on every run as a result. That warning is
cosmetic — nothing here creates a Compute resource — and enabling an unused API
to silence a log line is the wrong trade.

## Configuration

Everything is declared in stack config; `__main__.py` takes no per-project
branches.

```yaml
config:
  ol_gcp:projects:
  - project_id: mitol01
    business_unit: operations   # a BusinessUnit value
    region: us-east1
    enabled_services:
    - drive.googleapis.com
    - youtube.googleapis.com
    service_accounts:
    # No import_id: created fresh in mitol01, because a service account
    # cannot be moved between projects.
    - account_id: ocw-studio
      display_name: OCW Studio
      project_roles: []
    api_keys:
    # import_id: already lives in mitol01, so it is adopted rather than made.
    - key_name: learn-ai-production
      display_name: Learn AI Production
      restrictions:
        api_targets:
        - service: generativelanguage.googleapis.com
      import_id: projects/<project-number>/locations/global/keys/<key-name>
```

`import_id` present means "adopt what is already there"; absent means "create
it". Adopted resources are automatically marked `protect=True` — see
`adoption_opts` in `components/gcp/project.py` for why.

An API key created by this stack starts unprotected. Set `protect: true` on it
once a consumer holds the key string, because a replacement mints a new string
and breaks that consumer.

In practice `import_id` applies only to resources already resident in
`mitol01`. Service accounts, API keys and reCAPTCHA keys **cannot be moved
between GCP projects**, so consolidating one from a legacy project means
creating its replacement here and cutting the consumer over — a new email or a
new key string, every external grant re-issued by hand, and an application
deploy. `docs/plans/gcp-consolidation-into-mitol01.md` covers that sequence.

For an API key, the import id is the key's API resource `name` verbatim — read
it straight off `gcloud services api-keys list --format='value(name)'`. It is
*not* the `uid`, which is a separate output-only field that the CLI displays
more prominently. For Google-generated keys the two happen to carry the same
UUID, so reading the wrong one appears to work right up until it doesn't.

`restrictions` is mandatory on every API key. The component refuses an
unrestricted one rather than accepting the estate's current default of no
restriction at all.

## Credentials

The provider is always built through
`ol_infrastructure.lib.gcp.provider.gcp_provider()`, never from ambient
application-default credentials. Pulumi runs on Concourse workers and on
laptops that are routinely logged in to *some* Google account; inheriting
whichever identity happens to be present is how this estate came to be owned by
personal Gmail accounts.

The credential document lives in SOPS at `src/bridge/secrets/gcp/credentials.yaml`
under the key `credentials`, and may be either shape:

- `type: external_account` — Workload Identity Federation, exchanging the
  Concourse worker's AWS IAM role for a short-lived Google token. **Target
  state.** No key material at rest.
- `type: service_account` — a downloaded key. Accepted for bootstrapping only,
  because standing up federation needs an identity that predates it. Every use
  logs a warning naming the stack that still depends on one.

The live document is Workload Identity Federation against
`projects/32631020496/.../workloadIdentityPools/ol-infrastructure/providers/concourse`
in `mitol01`, impersonating `pulumi-gcp@mitol01.iam.gserviceaccount.com`. The
provider's attribute condition is `attribute.concourse_env == 'production'`, so
only the production Concourse `infra` worker pool can complete the exchange —
CI and QA are refused at the provider, not merely left unbound.

### Overriding the identity

`ol_gcp:impersonate_service_account` chains a second impersonation on top of
the federated credential, for the day a stack needs an account other than the
one the credential document names. The base account must then hold
`roles/iam.serviceAccountTokenCreator` on the target. Unset today.

### Running locally

The federated credential exchanges an *EC2 instance* identity for a Google
token, so it only resolves on a Concourse worker. A laptop has no instance
metadata service and the exchange fails. Impersonate instead:

```bash
gcloud auth application-default login
export OL_GCP_IMPERSONATE_SERVICE_ACCOUNT=pulumi-gcp@mitol01.iam.gserviceaccount.com
pulumi preview
```

It also skips the SOPS read entirely, so a preview does not require KMS access.

Impersonating rather than running as yourself is the point. A local preview
then sees exactly the permissions Concourse has, so a missing role surfaces on
your laptop instead of halfway through a pipeline run — and anyone with a
reason to run this locally holds Owner on `mitol01`, which would mask every
such gap. Confirmed empirically on 2026-08-19: `roles/owner` does **not**
confer `iam.serviceAccounts.getAccessToken`, so impersonation 403s until the
grant below exists. Owner is not a superset here.

#### Getting access

`roles/iam.serviceAccountTokenCreator` on `pulumi-gcp@mitol01` is what makes
the command above work. It belongs in `Pulumi.Production.yaml` under the
`pulumi-gcp` account's `iam_members`, granted to a Google group so onboarding
an engineer is a group-membership change rather than a Pulumi change.

It is not declared today. The first attempt named `group:odl-devops@mit.edu`,
and Google refused the binding with `Group odl-devops@mit.edu does not exist`,
which failed the production deploy. It comes back once a group Google actually
knows is chosen. Until then, anyone who needs a local preview gets the grant by
hand, and it should be recorded here when they do.

Service-account-level IAM does not appear in the project's IAM policy, so a
grant that is not written down in stack config exists only as a console click
nobody can reproduce.

## Kubernetes workloads calling Google APIs

The `eks-workloads` pool trusts each EKS cluster's service-account token
issuer, one OIDC provider per cluster, resolved from the cluster's EKS stack
(`eks_stack: data.Production`). A pod gets a Google token by exchanging a
projected Kubernetes token, so there is no key material at rest.

- The provider maps `google.subject` to `"<provider_id>::" + assertion.sub`.
  Principals are pool-scoped, and every cluster issues the same `sub` for the
  same namespace and service account, so without the prefix a grant meant for
  production would also admit CI.
- Grant `roles/iam.workloadIdentityUser` on a Google service account to
  `principal://iam.googleapis.com/projects/32631020496/locations/global/workloadIdentityPools/eks-workloads/subject/<provider_id>::system:serviceaccount:<namespace>:<name>`.
- The pod projects a token whose audience is
  `https://iam.googleapis.com/` + the provider's `name` (exported as
  `workload_identity_providers["<project>"]["<pool>/<provider>"]`), and points
  `GOOGLE_APPLICATION_CREDENTIALS`
  at an `external_account` document naming that token file. The Dagster stack's
  `ml` code location is the worked example.
- The binding is per Kubernetes service account, not per pod. Anything that
  runs as that service account can project the token, so scope the Kubernetes
  service account as narrowly as the Google grant should be.

## OCW YouTube Analytics for HighSystems

`ocw-yt-analytics-o7v08r` exists so Data Collaborative's HighSystems database can
pull per-video engaged-view stats for OCW's YouTube channel
([mitodl/hq#13305](https://github.com/mitodl/hq/issues/13305)). The stack enables
the YouTube Analytics, YouTube Reporting and YouTube Data APIs; everything else
is manual.

Step 1 must be done before the stack config naming this project merges.
`pulumi preview` plans the service creates without reading the project, so it
passes either way, but `pulumi up` fails on an ungranted project and that blocks
every other change in this one stack.

MIT Cloud Accounts created the project on 2026-10-09 (project number
`715788942366`, cost object `2010196`) in the same folder as `mitol01`, and
assigned the id suffix. It is a separate project so its OAuth consent screen
does not constrain `mitol01`'s. Cloud Accounts granted the requester
`roles/editor` and `roles/resourcemanager.projectIamAdmin`, not Owner, and
enabled no APIs.

1. Enable the bootstrap APIs (`roles/editor`) and grant the automation account
   on the new project (`roles/resourcemanager.projectIamAdmin`).
   `serviceUsageAdmin` alone is not enough: after enabling a service the
   provider reads the project back (`resourcemanager.projects.get`), which
   `roles/browser` supplies.

   ```bash
   gcloud services enable serviceusage.googleapis.com \
     cloudresourcemanager.googleapis.com --project=ocw-yt-analytics-o7v08r
   for role in roles/serviceusage.serviceUsageAdmin roles/browser; do
     gcloud projects add-iam-policy-binding ocw-yt-analytics-o7v08r \
       --member=serviceAccount:pulumi-gcp@mitol01.iam.gserviceaccount.com \
       --role="$role" --condition=None
   done
   ```

2. Deploy this stack.
3. Console > Google Auth Platform > Branding/Audience: configure the consent
   screen. User type follows the Google account that owns the OCW channel:
   _Internal_ if it is an `mit.edu` account (no verification needed),
   otherwise _External_, published to production. An External app left in
   _Testing_ issues refresh tokens that expire after 7 days, which breaks a
   scheduled sync. _Publish app_ refuses until Branding has `mit.edu` under
   authorized domains and the three App domain links filled in; the OCW home
   page and its privacy and terms page satisfy them. Leave the logo empty,
   because a logo puts the app through brand verification.
4. Console > Clients > Web application, with Data Collaborative's redirect URI.
   The secret is shown once; capture it into
   `src/bridge/secrets/gcp/ocw_highsystems_oauth.production.yaml` immediately,
   then hand the client id, secret and project id to Data Collaborative over a
   secure channel.

The channel is owned by `mitopencourseware@gmail.com`, so the app is
_External_ and published, as "OCW HighSystems". It is unverified, so the owner
clicks through an unverified-app warning once when connecting HighSystems. The
registered redirect URI carries an `accessToken` query parameter that Google
matches exactly, so if Data Collaborative changes that token the client and the
`redirect_uri` in the SOPS file have to be updated to match.

The channel owner then completes the OAuth flow in HighSystems with
`yt-analytics.readonly` and `youtube.readonly`. The second is not optional:
`reports.query` has required it since Google's 2018-06-18 revision, so a token
with only the analytics scope is rejected. An OAuth client with no token
requests for six months is deleted by Google, so a stalled integration loses
its client.

The consent screen's scope list does not limit what a client can request, so
whoever holds the client secret and controls a registered redirect URI can ask
any Google user for any scope under the OCW HighSystems name. Keep the
redirect URIs to Data Collaborative's single callback. Name the OL owner of the
client in its name. To cut access, rotate or delete the client secret
in the console, and have the channel owner revoke HighSystems at
<https://myaccount.google.com/permissions>.

## reCAPTCHA keys

Every OL property that uses reCAPTCHA renders the v2 checkbox through the
classic `api.js` and verifies the token through classic `siteverify`
(`https://www.google.com/recaptcha/api/siteverify`): mitxonline, xPro, Open
Discussions, mit-learn's HubSpot forms and the Keycloak olapps registration
form. The keys they use today are classic keys Google auto-migrated into
Enterprise, spread over `recaptcha-migrated-075600d5919`, `ol-data-platform`
and `mit-open`. A key cannot move between projects, and its site key is
rendered into the page, so each one is replaced by a key declared here and
the consumer is redeployed with it.

```yaml
    recaptcha_keys:
    - key_name: learn-rc
      display_name: Learn RC and CI
      integration_type: CHECKBOX
      allowed_domains:
      - rc.learn.mit.edu
      - ci.learn.mit.edu
```

- `integration_type: CHECKBOX` matches the v2 checkbox every consumer renders.
  `INVISIBLE` is v2 invisible and `SCORE` is v3. The provider replaces the key
  when this changes, which mints a new site key.
- `allowed_domains` is required. Subdomains are covered, so
  `rc.mitxonline.mit.edu` also covers `api.rc.mitxonline.mit.edu`.
- `testing_options` is never set: it makes every challenge pass (or fail).
  `waf_settings` is for keys a WAF (Fastly, Cloud Armor) issues, and
  `allow_amp_traffic` applies to `SCORE` only.
- Set `protect: true` once a deployed page renders the site key.

The site key is the key's resource id, exported as `recaptcha_site_keys`. It
is public, so it is exported in the clear.

### The legacy secret

Classic `siteverify` authenticates with the key's _legacy secret key_, which
Google supports for keys created in Enterprise
(<https://cloud.google.com/recaptcha/docs/verify>). The Pulumi provider has no
attribute or data source for it, and `gcloud` has no command, so it is read
from the `projects.keys.retrieveLegacySecretKey` API (or the key's page in the
console) and written into the consumer's secret store by hand. The caller
needs `recaptchaenterprise.keys.retrievelegacysecretkey` on `mitol01`, which
`roles/recaptchaenterprise.admin` carries.

```bash
set -o pipefail
site_key=$(pulumi stack output recaptcha_site_keys --json | jq -r '.mitol01["learn-rc"]')
secret=$(curl -sf \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "x-goog-user-project: mitol01" \
  "https://recaptchaenterprise.googleapis.com/v1/projects/mitol01/keys/${site_key}:retrieveLegacySecretKey" \
  | jq -er .legacySecretKey) || echo "legacy secret not retrieved" >&2
```

Stop if that printed an error rather than writing an empty value. Write
`$secret` straight into the store below (`sops edit`, or a `vault kv put`
of both fields for Open Discussions) without echoing it.

### Where each key is consumed

| Legacy key | Replacement | Consumer reads it from |
|---|---|---|
| MITx Online RC | `mitxonline-rc` | `src/bridge/secrets/mitxonline/secrets.{qa,ci}.yaml` `recaptcha.site-key`/`secret-key`, via Vault `secret-mitxonline/collected-static-secrets` |
| MITxPro RC and CI | `xpro-rc` | `src/bridge/secrets/xpro/secrets.{qa,ci}.yaml` `recaptcha.site_key`/`secret_key`, via Vault `secret-xpro/recaptcha` |
| MIT Open Discussions Pre-Prod | `open-discussions-rc` | Vault `secret-operations/rc-apps/mit-open/recaptcha-keys` (`site_key`, `secret_key`), written by hand and shared by the CI and QA Heroku apps |
| Learn RC (`mit-open`) | `learn-rc` | `src/bridge/secrets/mitlearn/secrets.qa.yaml` `recaptcha.site_key`/`secret_key`, read by `mit_learn` and, for the site key, `mit_learn_nextjs`. CI's values are empty. |
| SSO CI and RC (`ol-data-platform`) | `sso-rc` | Keycloak's own database (see below) |

Keycloak does not take its reCAPTCHA settings from this repository today.
`keycloak_realm:captcha_site_key` and `captcha_secret_key` are still set in
`substructure/keycloak/Pulumi.{CI,QA,Production}.yaml`, but nothing reads them
since the realm refactor in #3452 dropped the olapps registration flow and its
reCAPTCHA execution config. The registration pages on `sso-qa.ol.mit.edu` and
`sso.ol.mit.edu` still render the widget, so the live settings sit in the
olapps realm's registration flow in Keycloak. The SSO cutover is an admin
console change unless that flow is brought back under Pulumi first, which
should happen before the production SSO key is touched.

Bootcamp's three keys (Production, RC and CI) are deletions, not migrations:
`mitodl/bootcamp-ecommerce` was archived on 2026-08-17, the
`bootcamp*.odl.mit.edu` Heroku hostnames no longer resolve, and
`bootcamps.mit.edu` is a Webflow site with no reCAPTCHA. "MIT Open RC" in
`ol-data-platform` has no consumer in this repository; confirm its domains
before deleting it.

### Cutover, per property

Non-production keys go first, one property at a time, to prove these steps.
Production follows, and Keycloak SSO goes last because it guards the login of
every property behind SSO.

1. Declare the key under `recaptcha_keys` and let the pipeline apply it. Read
   the site key from `recaptcha_site_keys` and the legacy secret as above.
2. Write both values into the consumer's store for that tier and deploy the
   consumer (its Pulumi stack, then confirm the pods or Heroku dynos
   restarted).
3. Verify. The form page renders the new site key (`recaptchaKey` in the page
   settings for mitxonline, xPro and Open Discussions, `recaptchaSiteKey` in
   Keycloak's register page context), and one real submission (account
   creation, or a HubSpot form on mit-learn) goes through.
4. Set `protect: true` on the new key.
5. Delete the old key:
   `gcloud recaptcha keys delete <old-site-key> --project=<legacy-project>`.
   Classic verification leaves no usage signal on the GCP side, so "the old
   key went quiet" cannot be observed there. Check instead that no tier's page
   renders the old site key and no store still holds it.

Until step 5 the rollback is restoring the old values in the store and
redeploying; the old key keeps working.

## What this does not manage

- **The GCP projects themselves.** `mitol01` already exists, in folder
  `551004127831` under the MIT org. Whether further `mitol` projects can be
  provisioned, and against which cost object, is still open with IS&T —
  consolidating into the project we have does not wait on that.
- **Service-account keys.** Downloaded key material is the thing being removed.
  A workload that needs one gets federation instead; genuine exceptions are
  created by hand and recorded as such.
- **Generic OAuth 2.0 clients.** Google exposes no create, update or even
  *list* API for them. Hand-created, stored in Vault/SOPS, and — because they
  cannot be listed — dormant ones cannot be inventoried at all.
- **A reCAPTCHA key's legacy secret.** The provider has no attribute or data
  source for it; see [reCAPTCHA keys](#recaptcha-keys).
