# Agent Instructions for ol-infrastructure Repository

## Repository Overview

This is a **large-scale infrastructure-as-code monorepo** (467 Python files, ~91K lines of code) for managing MIT Open Learning's cloud infrastructure. The repository combines **Pulumi** for infrastructure provisioning, **Packer/PyInfra** for building AMIs, and **Concourse** for CI/CD pipelines—all managed through Python.

**Trust these instructions.** They are comprehensive and validated. Only search for additional information if these instructions are incomplete or incorrect.

## Essential Build & Validation Commands

### Environment Setup (One-Time)
```bash
# Install dependencies (ALWAYS run first after cloning)
uv sync

# Login to Pulumi state backend (required for Pulumi operations)
pulumi login s3://mitol-pulumi-state
```

### Linting & Formatting (Fast: <1 second)
```bash
# Format code (auto-fixes formatting issues)
uv run ruff format src/

# Check code style and quality (completes in <0.1s)
uv run ruff check src/

# Check code with auto-fixes
uv run ruff check --fix src/

# Format Packer HCL files
packer fmt -recursive src/bilder/
```

### Type Checking (Slow: ~75 seconds)
```bash
# Run mypy type checking (this is SLOW, budget 75+ seconds)
uv run mypy src/
```

**Note:** Expect ~1316 type errors and ~809 ruff errors in the codebase. Your changes should not introduce NEW errors.

### Pre-commit Hooks
```bash
# Run all pre-commit hooks (includes ruff, mypy, shellcheck, yamllint, etc.)
# Note: hadolint may fail on existing Docker issues—ignore if unrelated to your changes
uv run pre-commit run --all-files
```

### Testing
```bash
# Run tests (minimal test coverage exists)
uv run pytest tests/
```

## Critical Build Constraints

1. **Dependencies:** Use `uv` (v0.9.3+), NOT Poetry. README mentions Poetry but repository uses `uv`.
2. **Python Version:** Python 3.12.x required (specified in `pyproject.toml`)
3. **Linting:** Code MUST pass `ruff format` and minimize new `ruff check` errors
4. **Type Checking:** Run `mypy` but expect many existing errors (1316+)—only fix new ones
5. **Pre-commit:** Tests may fail on unrelated Docker linting—ignore if not your changes

## Repository Structure (What's Where)

### Core Source Directories (`src/`)

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

## Pulumi Project Structure

Each directory with a `Pulumi.yaml` is a **Pulumi project** (68 total). Example: `src/ol_infrastructure/infrastructure/aws/network/`

```
network/
├── Pulumi.yaml                                      # Project definition
├── __main__.py                                      # Entrypoint (deployment code)
├── Pulumi.infrastructure.aws.network.QA.yaml        # QA environment stack config
├── Pulumi.infrastructure.aws.network.Production.yaml # Production stack config
└── ...additional modules
```

**Stack naming convention:** `<namespace>.<environment>` where environment is `QA`, `Production`, `CI`, or `Dev`.

### Running Pulumi Commands

```bash
# Navigate to project directory
cd src/ol_infrastructure/infrastructure/aws/network/

# List available stacks
pulumi stack ls

# Select a stack
pulumi stack select infrastructure.aws.network.QA

# Preview changes (like terraform plan)
pulumi preview

# Deploy changes (CAREFUL: affects real infrastructure)
pulumi up

# Or run from repo root
pulumi -C src/ol_infrastructure/infrastructure/aws/network/ preview
```

**Key Pulumi Helpers:**
- `ol_infrastructure.lib.pulumi_helper.parse_stack()` — Returns `StackInfo` dataclass with stack name/environment
- `ol_infrastructure.lib.ol_types` — Pydantic models for configurations (inherit from `AWSBase`)

## Packer AMI Building

Packer images in `src/bilder/images/` use HCL definitions and PyInfra for provisioning.

**Build workflow (not typically run locally):**
1. Packer reads `.pkr.hcl` files (variables, builder config)
2. Launches EC2 instance from base AMI
3. Runs `deploy.py` using PyInfra to configure instance
4. Creates AMI snapshot

**Common operations:**
```bash
# Format Packer files
packer fmt src/bilder/images/

# Validate a Packer template (requires AWS credentials)
cd src/bilder/images/consul/
packer validate .
```

**PyInfra provisioning (`deploy.py`):** Pure Python scripts that define system state (install packages, configure services, etc.). See `src/bilder/images/consul/deploy.py` for example.

## Secrets Management (SOPS + Vault)

**Workflow:**
1. Secrets stored in git as SOPS-encrypted YAML files (`src/bridge/secrets/<context>/`)
2. SOPS config (`.sops.yaml`) defines KMS keys by environment (QA/Production/CI)
3. During `pulumi up`, `bridge.secrets.sops.set_env_secrets()` decrypts secrets in-memory
4. Pulumi writes secrets to HashiCorp Vault
5. Applications read secrets from Vault at runtime

**Required tools:** `sops` CLI (v3.11.0+), AWS credentials for KMS access

## Common Issues & Workarounds

### Issue: README mentions Poetry but `poetry.lock` doesn't exist
**Solution:** Repository has migrated to `uv`. Always use `uv sync` and `uv run <command>`.

### Issue: pre-commit hook `hadolint-docker` fails with existing Docker warnings
**Solution:** Ignore hadolint failures if your changes don't affect Dockerfiles. These are pre-existing issues.

### Issue: `mypy` reports 1316 errors
**Solution:** Expected. Only fix new errors introduced by your changes. Many legacy type issues exist.

### Issue: `ruff check` reports 809 errors
**Solution:** Expected. Focus on not introducing NEW errors. Consider using `--fix` for auto-fixable issues.

### Issue: Pulumi operation requires AWS credentials
**Solution:** Ensure AWS CLI is configured (`aws configure`) or environment variables are set.

### Issue: `uv` warns about `~=3.12` in `requires-python`
**Solution:** This is a warning, not an error. Ignore it—fixing requires changing project config.

### Issue: Timeout running commands
**Solution:**
- `mypy src/` takes 75+ seconds—increase timeout to 120s
- `uv run pytest` may need 60s timeout depending on test scope

## Code Style & Conventions

### Python
- **Type hints required:** All functions/methods must have type annotations
- **Pydantic for models:** Use `BaseModel` for data, `BaseSettings` for config
- **Imports:** Follow `ruff` isort rules (run `ruff format` to auto-fix)
- **Docstrings:** PEP 257 style (enforced by ruff)

### Pulumi
- **Projects:** Each `Pulumi.yaml` directory is a project
- **Stacks:** Named as `<namespace>.<environment>` (e.g., `infrastructure.aws.network.QA`)
- **Tags:** Use `ol_infrastructure.lib.ol_types.BusinessUnit` enum for OU tags
- **IAM policies:** Lint with `ol_infrastructure.lib.aws.iam_helper.lint_iam_policy()`

### Packer
- **HCL formatting:** Always run `packer fmt -recursive src/bilder/` before committing
- **Components:** Reuse components from `src/bilder/components/` instead of duplicating logic

## File Modification Cheatsheet

| Task | Files to Modify | Validation |
|------|----------------|------------|
| Add AWS resource | `src/ol_infrastructure/infrastructure/aws/<service>/__main__.py` | `pulumi preview` |
| Create new app infra | Create `src/ol_infrastructure/applications/<app>/` with `Pulumi.yaml` + `__main__.py` | `pulumi stack init` then `preview` |
| Add reusable component | `src/ol_infrastructure/components/<category>/` | Used in other projects |
| Modify AMI build | `src/bilder/images/<image>/deploy.py` or `.pkr.hcl` | Packer build (CI only) |
| Add CI/CD pipeline | `src/ol_concourse/pipelines/<category>/` | Generate YAML, deploy to Concourse |
| Add secret | Encrypt with `sops`, place in `src/bridge/secrets/<context>/`, decrypt in Pulumi | `sops` CLI |

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

## Validation Checklist

Before submitting changes:
1. ✅ `uv sync` — Ensure dependencies are installed
2. ✅ `uv run ruff format src/` — Auto-format code
3. ✅ `uv run ruff check src/` — Check for new linting errors
4. ✅ `uv run mypy src/` — Verify no new type errors (75s runtime)
5. ✅ `packer fmt -recursive src/bilder/` — Format Packer files (if modified)
6. ✅ `pulumi preview` — Validate Pulumi changes (if applicable)
7. ✅ `uv run pytest tests/` — Run tests (if applicable)

**Optional but recommended:**
- `uv run pre-commit run --all-files` — Run all hooks (may take 2+ minutes)

## When to Search vs. Trust Instructions

**Trust instructions for:**
- Build commands (validated and documented above)
- Tool versions (verified in environment)
- Repository structure (exhaustively mapped)
- Common issues (based on actual testing)

**Search/explore when:**
- Finding specific Pulumi resource implementations
- Understanding complex component logic
- Tracing dependencies between projects
- Debugging unfamiliar error messages
