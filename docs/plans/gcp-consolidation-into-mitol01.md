# Consolidating the GCP estate into mitol01

Status: spec. Supersedes the in-place adoption framing of the same document
(previously `gcp-pulumi-import-strategy.md`), which assumed resources would be
adopted where they sit.
Evidence base: `docs/plans/gcp-service-account-consumer-map.md` (discovery,
2026-08-10), plus live inspection of `mitol01` on 2026-08-19.
Implementation: `src/ol_infrastructure/infrastructure/gcp/`,
`src/ol_infrastructure/components/gcp/project.py`,
`src/ol_infrastructure/lib/gcp/provider.py`.

## The decision that reframes everything

`mitol01` is the destination. Credentials are **re-created there and cut over**,
not adopted where they sit.

That distinction is not a matter of preference. **GCP service accounts, API
keys and reCAPTCHA keys cannot be moved between projects.** There is no API for
it and no Pulumi resource that expresses it. So for everything outside
`mitol01` the operation is not an import at all:

```
create the replacement in mitol01
  -> re-establish its external grants
  -> cut the consumer over to the new credential
  -> verify the old one goes quiet
  -> delete the old one
```

`pulumi import` applies only to what is *already* in `mitol01` — today the four
API keys and the `google-ads-optimization@` service account. Everything else is
ordinary resource creation on the Pulumi side, and the hard part lives entirely
outside Pulumi.

### What that costs, stated plainly

A re-created service account has a **new email address**. Every grant that
named the old one has to be re-issued:

- Shared Drive memberships (`0AErNBMZMmOz3Uk9PVA`, `0AAx-gQtx1vVKUk9PVA`,
  `0AIZerpz9jimTUk9PVA`)
- Sheet and Drive-folder shares (xPro coupon/refund/deferral, the Canvas sheet
  `13AoothEhEvWs2cJEEfZETm7E6h3-ZY4tD11KX_ARe1A`)
- BigQuery dataset and GCS bucket access granted by **edx.org**, a third party
- YouTube channel permissions, Analytics properties, Ads links

None of these are visible to any GCP API, and several are granted by people
outside OL. `tk-enumerate-external-google-product-grants-per-cre-070a83` is
therefore not background research — it is the critical path. Each credential's
re-home is gated on knowing its grants, and each grant is manual work in a
different product's admin UI.

A re-created API key has a **new key string**, and a re-created reCAPTCHA key a
**new site key**. Both live in application config, so every re-home is a
coordinated application deploy, not an infrastructure change.

### What does not survive the move at all

Two things are non-transferable and must be decided before, not during:

- **Granted quota.** `ocw-studio-qa` holds a YouTube quota of 210,000/day (21×
  default) and `mit-open` 100,000/day (10×). These are audited grants attached
  to the project. A new project starts at Google's default of 10,000/day and a
  fresh quota request. Re-homing either of these consumers without a granted
  increase on `mitol01` in hand first will throttle them.
- **Generic OAuth 2.0 clients.** No create, update, or even *list* API exists.
  They are hand-created in the console, and dormant ones cannot be inventoried
  at all. Three are known only because they showed traffic.

## What Pulumi manages, and what it cannot

| Type | Pulumi resource | Moves between projects? |
|---|---|---|
| Enabled service (API) | `gcp.projects.Service` | n/a — just enable it on `mitol01` |
| Service account | `gcp.serviceaccount.Account` | **no** — re-create, new email |
| Project IAM grant | `gcp.projects.IAMMember` | n/a — re-declare |
| API key | `gcp.projects.ApiKey` | **no** — re-create, new key string |
| reCAPTCHA key | `gcp.recaptcha.EnterpriseKey` | **no** — re-create, new site key |
| Generic OAuth client | **none** | no API of any kind |
| Service-account key (JSON) | `gcp.serviceaccount.Key` | **deliberately unmanaged** |

Service-account keys are managed by not existing. `gcp.serviceaccount.Key`
would put private key material into Pulumi state, and removing long-lived key
material is the point of the exercise. Workloads authenticate through Workload
Identity Federation; anything that genuinely cannot is created by hand and
recorded as an exception.

## Import identifiers, for the mitol01-resident minority

Verified 2026-08-19 against the upstream `terraform-provider-google`
documentation. The Pulumi Python SDK ships empty docstrings for these
resources, so the provider's own docs are the source.

| Resource | Import id format | How to get the id |
|---|---|---|
| `gcp.projects.Service` | `{{project_id}}/{{service}}` | `gcloud services list --enabled --project=P` |
| `gcp.serviceaccount.Account` | `projects/{{project_id}}/serviceAccounts/{{email}}` | `gcloud iam service-accounts list --project=P` |
| `gcp.projects.IAMMember` | `"{{project_id}} {{role}} {{member}}"` — **space-delimited** | `gcloud projects get-iam-policy P` |
| `gcp.projects.ApiKey` | `projects/{{project}}/locations/global/keys/{{name}}` | `gcloud services api-keys list --format='value(name)'` |
| `gcp.recaptcha.EnterpriseKey` | `projects/{{project}}/keys/{{name}}` | `gcloud recaptcha keys list --project=P` |

Two traps:

- The IAM member id is **space**-delimited, unlike every other GCP import id:
  `"mitol01 roles/viewer user:foo@example.com"`.
- The API key id ends with the key's **`name`** — read it verbatim off
  `--format='value(name)'`, which returns the whole
  `projects/N/locations/global/keys/ID` path. It is *not* the `uid`, a separate
  output-only field the CLI displays more prominently. For Google-generated
  keys both happen to carry the same UUID, so reading the wrong one appears to
  work right up until a key whose `name` was chosen by hand.

Adoption is declared, not performed out of band: `import_id` in stack config
becomes `ResourceOptions(import_=..., protect=True)`. `pulumi import` on the
command line mutates state invisibly — nothing in the repository records that
it happened, and a reviewer cannot tell an adopted resource from a new one.

## Safety rules

Each is a reading of a specific discovery finding, and each is enforced in
`components/gcp/project.py` rather than left as prose.

1. **Never disable a service on destroy.** `disable_on_destroy=False` is set
   explicitly on every `gcp.projects.Service`. Removing a service from the
   stack must not turn the API off for a consumer the stack does not know
   about — and several consumers emit no GCP-side signal at all. reCAPTCHA
   classic verification goes to `www.google.com/recaptcha/` and produces no
   `request_count`, no last-auth, no metric of any kind; GCS reads do not
   report caller-side `request_count` either. **Zero traffic is never proof of
   death**, and during a cutover it is also not proof that the old credential
   is safe to delete — see the verification step below.

2. **Never use `gcp.projects.IAMPolicy` or `IAMBinding`.** Both are
   authoritative: they delete grants they do not declare. Legacy projects carry
   direct Owner grants held by personal Gmail accounts, and removing those as a
   side effect of an unrelated change would lock out the only identity that can
   currently see some of these projects. `IAMMember` is non-authoritative and
   is the only IAM resource this component uses.

3. **API keys must declare restrictions.** `OLGCPAPIKeyConfig` refuses an
   unrestricted key. Six keys in the legacy estate carry none at all, including
   the only unrestricted key actually serving traffic. Since replacements are
   created fresh, there is no reason for a single unrestricted key to exist in
   `mitol01`.

4. **Adopt only the services in use.** A project typically has 20–40 enabled,
   most of them Google defaults nobody chose. Rule 1 means leaving them
   unmanaged turns nothing off.

5. **`protect=True` travels with every import.** Dropped deliberately, per
   resource, never in bulk.

## Order of work

1. **Establish `mitol01` as managed.** Adopt what is already there — four API
   keys, the ads service account, the three services with a proven consumer.
   This proves the provider, the federated credential and the import path
   against resources whose blast radius is understood. It is the current stack
   config.
2. **Enumerate external grants per credential.** The critical path. Nothing
   below can be scheduled without it.
3. **Secure quota before moving the consumers that depend on it.** Request the
   YouTube increases on `mitol01` and have them granted before touching
   `ocw-studio-qa` or `mit-open`. This has a lead time outside our control.
4. **Re-home credential by credential, cheapest first.** For each: create in
   `mitol01`, re-issue grants, deploy the consumer against the new credential,
   watch the old credential go quiet for a full duty cycle, then delete it.
   Order within this step is set by consumer risk, not by project.
5. **Delete the emptied legacy projects.** Only once every credential in them
   is confirmed dead — by the absence of traffic *and* a known consumer having
   been cut over, never by absence alone.

The verification in step 4 is the one that cannot be skipped or shortened.
A credential whose consumer is a cron that runs monthly will look dead for
weeks. The consumer map records the observed traffic shape for each live
credential; use it to choose the watch window rather than a fixed interval.

## Open questions

- **`Translations Experimentation`** (API key in `mitol01`, created
  2025-12-30, restricted to `generativelanguage` + `aiplatform`) has no
  identified consumer. It is adopted rather than deleted pending an answer.
- **Which legacy projects are consolidation targets versus deletions?** The
  consumer map divides them by liveness, not by intent. Step 5 needs that call.
- **What are IS&T's constraints on `mitol01`** — cost objects, folder-level org
  policy, and whether additional `mitol` projects can be provisioned if one
  project turns out to be the wrong granularity. →
  `tk-establish-with-is-amp-t-what-ol-can-do-above-pro-bf8bdd`
