---
name: new-pulumi-project
description: >
  Step-by-step guide for scaffolding a new Pulumi project in ol-infrastructure.
  Use when creating a new infrastructure, application, or substructure Pulumi
  project, including stack initialization and optional Terraform provider setup.
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

```bash
copier copy copier_templates/pulumi_project/ src/ol_infrastructure/<project_type>/<project_name>
```

The generated `Pulumi.yaml` backend is pre-set to `s3://mitol-pulumi-state/`.

### 3. Initialize stacks

Navigate into the new project directory and initialize CI, QA, and Production
stacks. Each stack uses a KMS key scoped to its environment.

```bash
cd src/ol_infrastructure/<project_type>/<project_name>

pulumi stack init <project_type>.<project_name>.CI \
  --secrets-provider=awskms://alias/infrastructure-secrets-ci

pulumi stack init <project_type>.<project_name>.QA \
  --secrets-provider=awskms://alias/infrastructure-secrets-qa

pulumi stack init <project_type>.<project_name>.Production \
  --secrets-provider=awskms://alias/infrastructure-secrets-production
```

Stack naming convention: `<project_type>.<project_name>.<Environment>` where
Environment is `CI`, `QA`, or `Production` (capitalization matters — it is
interpolated into tag values).

### 4. Set up encrypted secrets

Create a SOPS-encrypted secrets file for the new project:

```bash
mkdir src/bridge/secrets/<project_name>
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
