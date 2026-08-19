# GCP infrastructure stacks

Pulumi management of Google Cloud Platform projects, credentials and enabled
APIs. This is the landing point for the work tracked in
`docs/plans/gcp-consolidation-into-mitol01.md`, which is where the *why* of every
convention below is argued out. The credential inventory those decisions rest
on is `docs/plans/gcp-service-account-consumer-map.md`.

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
the command above work. It is granted to a **group**, declared in
`Pulumi.Production.yaml` under the `pulumi-gcp` account's `iam_members` —
so onboarding an engineer is a group-membership change, not a Pulumi change,
and no one needs to remember a `gcloud add-iam-policy-binding` incantation.

Both grants on that account are declared there: the `workloadIdentityUser`
binding for the Concourse principal set, and the `serviceAccountTokenCreator`
binding for people. Service-account-level IAM does not appear in the project's
IAM policy, so if it is not written down here it exists only as a console click
nobody can reproduce.

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
- **reCAPTCHA keys.** Manageable via `gcp.recaptcha.EnterpriseKey`, but the
  site key ships inside deployed frontends, so rotation is an application
  release. Deferred until the app-side cutover is designed.
