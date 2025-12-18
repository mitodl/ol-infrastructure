# Repository Overview

This is a **large-scale infrastructure-as-code monorepo** (467 Python files, ~91K lines of code) for managing MIT Open Learning's cloud infrastructure. The repository combines **Pulumi** for infrastructure provisioning, **Packer/PyInfra** for building AMIs, and **Concourse** for CI/CD pipelines—all managed through Python.

## Core Structure

### Source Directories (`src/`)

```
src/ol_infrastructure/         # Pulumi infrastructure-as-code (68 projects)
├── applications/              # App-specific infrastructure (40+ apps)
│   ├── airbyte/              # Example: Airbyte deployment
│   ├── mit_learn/            # Example: MIT Learn platform
│   └── ...                   # Each dir is a Pulumi project
├── infrastructure/            # Foundational infrastructure
│   ├── aws/                  # AWS resources (networking, IAM, EKS, etc.)
│   ├── vault/                # HashiCorp Vault setup
│   └── monitoring/           # Monitoring infrastructure
├── components/                # Reusable Pulumi components
│   ├── aws/                  # AWS component resources
│   └── services/             # Service abstractions
├── lib/                       # Helper libraries
│   ├── aws/                  # AWS helpers (IAM, EC2, RDS, etc.)
│   ├── pulumi_helper.py      # Stack parsing utilities
│   └── ol_types.py           # Pydantic models & enums
└── substructure/             # Supporting infrastructure

src/bilder/                    # Packer AMI building (21 HCL files)
├── components/               # Reusable provisioning components
│   ├── hashicorp/           # Consul, Vault installers
│   ├── vector/              # Vector log agent
│   └── baseline/            # Base system setup
├── images/                  # AMI definitions
│   ├── consul/              # Example: Consul server AMI
│   │   ├── deploy.py        # PyInfra provisioning script
│   │   ├── files/           # Static files
│   │   └── templates/       # Config templates
│   └── ...                  # Each dir is a Packer build

src/ol_concourse/             # Concourse CI/CD pipeline generation
├── lib/                     # Pipeline generation library
│   ├── models/              # Pydantic models for Concourse resources
│   └── jobs/                # Job templates
└── pipelines/               # Pipeline definitions
    ├── infrastructure/      # Infra deployment pipelines
    └── applications/        # App deployment pipelines

src/bridge/                   # Shared utilities
├── secrets/                 # SOPS secret handling
├── lib/versions.py          # Version constants
└── settings/                # Shared settings
```

### Configuration Files (Root)

- `pyproject.toml` — Python dependencies (uv format), ruff/mypy config
- `.pre-commit-config.yaml` — Pre-commit hook definitions
- `.sops.yaml` — SOPS encryption rules (KMS key mappings)
- `uv.lock` — Locked dependency versions

## Environment Requirements

**Installed tools (verified available):**
- Python 3.12.7
- uv 0.9.3
- Pulumi 3.199.0
- Packer 1.14.2
- AWS CLI 2.31.17
- Vault CLI 1.20.4
- SOPS 3.11.0
- PyInfra 3.5.1 (via uv)

**Not required locally:** Consul CLI (used in deployed infrastructure only)

## Key Files Reference

- `pyproject.toml` — Dependencies, tool config (ruff/mypy settings)
- `.pre-commit-config.yaml` — Pre-commit hooks (ruff, mypy, yamllint, shellcheck, packer fmt, hadolint)
- `README.md` — High-level overview (mentions outdated Poetry workflow)
- `src/ol_infrastructure/lib/pulumi_helper.py` — Stack parsing utilities
- `src/ol_infrastructure/lib/ol_types.py` — Core Pydantic types, enums
- `src/bridge/secrets/sops.py` — SOPS decryption helper
- `.sops.yaml` — SOPS encryption rules
- `docs/adr/` — Architecture Decision Records

## Critical Build Constraints

1. **Dependencies:** Use `uv` (v0.9.3+), NOT Poetry. README mentions Poetry but repository uses `uv`.
2. **Python Version:** Python 3.12.x required (specified in `pyproject.toml`)
3. **Linting:** Code MUST pass `ruff format` and minimize new `ruff check` errors
4. **Type Checking:** Run `mypy` but expect many existing errors (1316+)—only fix new ones
5. **Pre-commit:** Tests may fail on unrelated Docker linting—ignore if not your changes

**Note:** Expect ~1316 type errors and ~809 ruff errors in the codebase. Your changes should not introduce NEW errors.

## When to Trust These Instructions

**Trust instructions for:**
- Build commands (validated and documented)
- Tool versions (verified in environment)
- Repository structure (exhaustively mapped)
- Common issues (based on actual testing)

**Search/explore when:**
- Finding specific Pulumi resource implementations
- Understanding complex component logic
- Tracing dependencies between projects
- Debugging unfamiliar error messages
