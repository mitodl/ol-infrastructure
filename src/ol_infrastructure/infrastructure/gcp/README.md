# GCP infrastructure stacks

Pulumi management of Google Cloud Platform projects, credentials and enabled
APIs. This is the landing point for the work tracked in
`docs/plans/gcp-pulumi-import-strategy.md`, which is where the *why* of every
convention below is argued out. The credential inventory those decisions rest
on is `docs/plans/gcp-service-account-consumer-map.md`.

## Stack layout

One stack per GCP project, named `<tenant>.<Environment>`:

```
ocw-studio.Production      -> project ocw-studio-production
ocw-studio.QA              -> project ocw-studio-qa
mit-learn.Production       -> project mit-open
```

The stack name states the intent; `gcp_project:project_id` states the literal
GCP project the stack currently maps to. They differ today on purpose — the
legacy estate's project names are not trustworthy (`ocw-studio-qa` carries
production YouTube publishing and the estate's largest granted quota;
`recaptcha-migrated-075600d5919` is machine-generated). Keeping both means the
truth is written down in one file per stack rather than inferred from a name.

```bash
cd src/ol_infrastructure/infrastructure/gcp/
pulumi stack select ocw-studio.Production
pulumi preview
```

## Configuration

Everything is declared in stack config; `__main__.py` takes no per-project
branches.

```yaml
config:
  gcp_project:project_id: ocw-studio-production
  gcp_project:business_unit: open-courseware   # a BusinessUnit value
  gcp_project:region: us-east1
  gcp_project:enabled_services:
  - drive.googleapis.com
  - youtube.googleapis.com
  gcp_project:service_accounts:
  - account_id: ocw-studio-production
    display_name: OCW Studio Production
    project_roles: []
    import_id: projects/ocw-studio-production/serviceAccounts/ocw-studio-production@ocw-studio-production.iam.gserviceaccount.com
  gcp_project:api_keys:
  - key_name: youtube-production
    display_name: OCW Studio YouTube key
    restrictions:
      api_targets:
      - service: youtube.googleapis.com
    # <key-name> is the last segment of the key's API resource name, not its
    # separate uid field. `gcloud services api-keys list --format=json`.
    import_id: projects/<project-id-or-number>/locations/global/keys/<key-name>
```

`import_id` present means "adopt what is already there"; absent means "create
it". Adopted resources are automatically marked `protect=True` — see
`adoption_opts` in `components/gcp/project.py` for why.

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
