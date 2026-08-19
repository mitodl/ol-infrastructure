# GCP infrastructure stacks

Pulumi management of Google Cloud Platform projects, credentials and enabled
APIs. This is the landing point for the work tracked in
`docs/plans/gcp-pulumi-import-strategy.md`, which is where the *why* of every
convention below is argued out. The credential inventory those decisions rest
on is `docs/plans/gcp-service-account-consumer-map.md`.

## Stack layout

Three stacks — `CI`, `QA`, `Production` — each managing every GCP project whose
credentials serve that tier.

```bash
cd src/ol_infrastructure/infrastructure/gcp/
pulumi stack select Production
pulumi preview
```

Not one stack per GCP project. The GCP project *is* already the environment
boundary — `ocw-studio-production` and `ocw-studio-qa` are separate projects —
so a stack-per-project layout would encode that boundary twice, and encode it
wrongly: several legacy project names lie about their tier. `ocw-studio-qa`
carries production YouTube publishing and the estate's largest granted quota,
and `recaptcha-migrated-075600d5919` is machine-generated. The stack name
states the tier the credentials actually serve; `project_id` states which GCP
project happens to hold them today.

One GCP project can therefore appear in more than one stack — `mitol01` holds
Learn AI keys for all three tiers. When it does, **`enabled_services` belongs
to the `Production` stack only**: an enabled API is a property of the project,
not of a tier, so declaring it in two stacks would put two Pulumi resources in
conflict over one API.

The stack boundary separates what is *declared*. It does not by itself separate
what the deploying identity can *do* — all three stacks share one automation
account unless `ol_gcp:impersonate_service_account` gives a stack its own. See
"Credentials" below.

## Configuration

Everything is declared in stack config; `__main__.py` takes no per-project
branches.

```yaml
config:
  # optional: per-stack automation identity, see Credentials
  ol_gcp:impersonate_service_account: pulumi-gcp-qa@mitol01.iam.gserviceaccount.com
  ol_gcp:projects:
  - project_id: ocw-studio-production
    business_unit: open-courseware   # a BusinessUnit value
    region: us-east1
    enabled_services:
    - drive.googleapis.com
    - youtube.googleapis.com
    service_accounts:
    - account_id: ocw-studio-production
      display_name: OCW Studio Production
      project_roles: []
      import_id: projects/ocw-studio-production/serviceAccounts/ocw-studio-production@ocw-studio-production.iam.gserviceaccount.com
    api_keys:
    - key_name: youtube-production
      display_name: OCW Studio YouTube key
      restrictions:
        api_targets:
        - service: youtube.googleapis.com
      import_id: projects/<project-number>/locations/global/keys/<key-name>
```

`import_id` present means "adopt what is already there"; absent means "create
it". Adopted resources are automatically marked `protect=True` — see
`adoption_opts` in `components/gcp/project.py` for why.

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

### Per-stack identities

All three stacks share `pulumi-gcp@` by default, which means the `CI` stack
holds `projectIamAdmin` on production projects. To make the stack boundary a
permission boundary, create one account per tier, grant each roles only on its
own projects, and set `ol_gcp:impersonate_service_account` in that stack. The
federated credential impersonates the base account, which then chains to the
tier account — so the base account needs
`roles/iam.serviceAccountTokenCreator` on each tier account, and no second
credential document is needed.

### Running locally

The federated credential exchanges an *EC2 instance* identity for a Google
token, so it only resolves on a Concourse worker. A laptop has no instance
metadata service and the exchange fails. Impersonate instead:

```bash
gcloud auth application-default login
export OL_GCP_IMPERSONATE_SERVICE_ACCOUNT=pulumi-gcp@mitol01.iam.gserviceaccount.com
pulumi preview
```

This needs `roles/iam.serviceAccountTokenCreator` on that service account. It
also skips the SOPS read entirely, so a preview does not require KMS access.

Impersonating rather than running as yourself is the point. A local preview
then sees exactly the permissions Concourse has, so a missing role surfaces on
your laptop instead of halfway through a pipeline run — and anyone with a
reason to run this locally holds Owner on `mitol01`, which would mask every
such gap.

## What this does not manage

- **The GCP projects themselves.** Creating a project needs an org/folder
  parent and a billing account, and where OL's projects are allowed to sit is
  still open with IS&T. Adopting the contents of existing projects does not
  wait on that.
- **Service-account keys.** Downloaded key material is the thing being removed.
  A workload that needs one gets federation instead; genuine exceptions are
  created by hand and recorded as such.
- **Generic OAuth 2.0 clients.** Google exposes no create, update or even
  *list* API for them. Hand-created, stored in Vault/SOPS, and — because they
  cannot be listed — dormant ones cannot be inventoried at all.
- **reCAPTCHA keys.** Manageable via `gcp.recaptcha.EnterpriseKey`, but the
  site key ships inside deployed frontends, so rotation is an application
  release. Deferred until the app-side cutover is designed.
