---
name: new-pulumi-project
description: >
  Step-by-step guide for scaffolding a new Pulumi project in ol-infrastructure.
  Use when creating a new infrastructure, application, or substructure Pulumi
  project, including stack initialization.
---

# Creating a New Pulumi Project

## When to use

Use this skill whenever you need to create a new Pulumi project in the
ol-infrastructure monorepo — whether it's an `infrastructure`, `application`,
or `substructure` project.

## Prerequisites

- You are at the root of the `ol-infrastructure` repo
- `copier`, `pulumi`, and `uv` are installed and on your PATH
- You are logged in to Pulumi: `pulumi login s3://mitol-pulumi-state`

## Steps

### 1. Create a feature branch

```bash
git checkout -b <branch-name>
```

### 2. Scaffold the project with copier

Run from the repo root. Copier will interactively prompt for:
- **project_name** — snake_case name used for the directory and stack prefix (e.g. `qdrant_cloud`)
- **project_type** — one of `application`, `substructure`, or `infrastructure`
- **project_description** — human-readable description for `Pulumi.yaml`

You should choose a **target module path** under `src/ol_infrastructure` that matches the
existing directory layout for the kind of project you're creating. For example:

- Application: `applications/<project_name>`
- Infrastructure: `infrastructure/aws/<project_name>` (or another direct child path that matches existing projects)
- Substructure: `substructure/<project_name>` (or a more specific nested path under an existing substructure hierarchy, if applicable)

Use that module path as the copier destination:

```bash
copier copy copier_templates/pulumi_project/ src/ol_infrastructure/<target_module_path>
```

The generated `Pulumi.yaml` backend is pre-set to `s3://mitol-pulumi-state/`.

### 3. Initialize stacks

Navigate into the new project directory and initialize CI, QA, and Production
stacks. Each stack uses a KMS key scoped to its environment.

```bash
cd src/ol_infrastructure/<target_module_path>

# <namespace> should match the dotted module path for this project,
# for example: applications.mitxonline or infrastructure.aws.myproject
pulumi stack init <namespace>.CI \
  --secrets-provider=awskms://alias/infrastructure-secrets-ci

pulumi stack init <namespace>.QA \
  --secrets-provider=awskms://alias/infrastructure-secrets-qa

pulumi stack init <namespace>.Production \
  --secrets-provider=awskms://alias/infrastructure-secrets-production
```

Stack naming convention: `<namespace>.<Environment>` where `namespace` matches
the dotted module path for the project (for example,
`applications.mitxonline` or `infrastructure.aws.myproject`) and Environment is
`CI`, `QA`, or `Production` (capitalization matters — it is interpolated into
tag values).

### 4. Set up encrypted secrets

Create a SOPS-encrypted secrets file for the new project:

```bash
cd "$(git rev-parse --show-toplevel)"  # return to repo root regardless of nesting depth
mkdir -p src/bridge/secrets/<project_name>
cd src/bridge/secrets/<project_name>
sops account.yaml             # or whatever the secret file should be named
```

Consult `.sops.yaml` at the repo root to confirm the correct KMS key rules
apply to this path before editing.

## Stack config files

After stack init, create a `Pulumi.<stack-name>.yaml` for each environment with
the stack's configuration values. See neighbouring projects for examples of the
expected keys.

## Checklist

- [ ] Feature branch created
- [ ] `copier copy` scaffolded `Pulumi.yaml`, `__main__.py`, `__init__.py`
- [ ] Three stacks initialized (CI, QA, Production) with per-env KMS keys
- [ ] `src/bridge/secrets/<project_name>/` directory and SOPS file created
- [ ] Per-stack `Pulumi.<stack>.yaml` config files populated
