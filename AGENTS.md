# Agent Instructions for ol-infrastructure Repository

This is a **large-scale infrastructure-as-code monorepo** (467 Python files, ~91K lines of code) for managing MIT Open Learning's cloud infrastructure using Pulumi, Packer/PyInfra, and Concourse.

Scoped `AGENTS.md` files exist inside `src/bilder/`, `src/ol_concourse/`, and `src/ol_infrastructure/` — read those when working inside those packages. This root file covers repo-wide concerns only.

---

## Quick Start

```bash
# Environment setup (one-time)
uv sync
pulumi login s3://mitol-pulumi-state

# Validate your changes
uv run ruff format src/        # Auto-format code
uv run ruff check src/         # Check code quality
uv run mypy src/               # Type check (75s runtime)
uv run pytest tests/           # Run tests
```

---

## Documentation Index

The AGENTS.md instructions have been organized into focused documents. Read the relevant sections for your task:

### 📋 [Repository Overview](docs/context/01-repository-overview.md)

- Repository structure and organization
- Directory layout and purpose
- Environment requirements
- Critical build constraints

**Use when:** Understanding the codebase structure or finding where code should live

### 🔨 [Build and Validation](docs/context/02-build-and-validation.md)

- Environment setup and dependency installation
- Linting, formatting, and type checking commands
- Pre-commit hooks and validation checklist
- Common build issues and solutions

**Use when:** Setting up environment, validating code, or fixing build errors

### 🔄 Pulumi Workflows → [`src/ol_infrastructure/AGENTS.md`](src/ol_infrastructure/AGENTS.md)

Directory roles, stack naming, component rules, key helpers, validation.
Deep reference: [docs/context/03-pulumi-workflows.md](docs/context/03-pulumi-workflows.md)

### 📦 Packer AMI Building → [`src/bilder/AGENTS.md`](src/bilder/AGENTS.md)

Build chain, PyInfra conventions, component reuse, HCL formatting.
Deep reference: [docs/context/04-packer-bilder.md](docs/context/04-packer-bilder.md)

### 🔄 Concourse Pipelines → [`src/ol_concourse/AGENTS.md`](src/ol_concourse/AGENTS.md)

Pipeline DSL, factory functions, Identifier rules, meta pipeline registration.

### 🔐 [Secrets Management](docs/context/05-secrets-management.md)

- SOPS encryption and KMS integration
- Working with encrypted secrets in git
- Pulumi secret integration
- Vault runtime secret access
- Secret file organization

**Use when:** Managing secrets, encrypting sensitive data, configuring SOPS

### 🎨 [Code Style & Conventions](docs/context/06-code-style-conventions.md)

- Python type hints and Pydantic models
- Import organization and formatting
- Pulumi conventions (projects, stacks, components)
- Packer HCL and PyInfra style
- Configuration management patterns

**Use when:** Writing code, reviewing pull requests, establishing patterns

### 🧪 [Testing Strategy](docs/context/07-testing-strategy.md)

- When to write unit, integration, and policy tests
- Unit testing with Pulumi mocks
- Integration testing with Automation API
- Test organization and best practices
- Testing checklist for AI agents

**Use when:** Creating tests, validating infrastructure code, testing components

### 🏗️ [Architecture Decisions (ADR)](docs/context/08-architecture-decisions.md)

- When to create Architecture Decision Records
- ADR creation process and template
- Best practices for documenting decisions
- Integration with development workflow

**Use when:** Making significant architectural changes, evaluating options

### 🐛 [Troubleshooting](docs/context/09-troubleshooting.md)

- Common build, linting, and type-checking issues
- Pulumi, Packer, and secrets management problems
- Testing and timeout issues
- Debugging tips and getting help

**Use when:** Encountering errors, investigating problems, debugging

---

## File Modification Quick Reference

| Task | Files to Modify | Guide |
|------|----------------|-------|
| Add AWS resource | `src/ol_infrastructure/infrastructure/aws/<service>/__main__.py` | [`src/ol_infrastructure/AGENTS.md`](src/ol_infrastructure/AGENTS.md) |
| Create new app | `src/ol_infrastructure/applications/<app>/` | [`src/ol_infrastructure/AGENTS.md`](src/ol_infrastructure/AGENTS.md) |
| Add reusable component | `src/ol_infrastructure/components/<category>/` | [`src/ol_infrastructure/AGENTS.md`](src/ol_infrastructure/AGENTS.md) + [Testing](docs/context/07-testing-strategy.md) |
| Modify AMI build | `src/bilder/images/<image>/deploy.py` | [`src/bilder/AGENTS.md`](src/bilder/AGENTS.md) |
| Add CI/CD pipeline | `src/ol_concourse/pipelines/<category>/` | [`src/ol_concourse/AGENTS.md`](src/ol_concourse/AGENTS.md) |
| Manage secrets | `src/bridge/secrets/<context>/` | [Secrets Management](docs/context/05-secrets-management.md) |
| Document architecture | `docs/adr/NNNN-title.md` | [Architecture Decisions](docs/context/08-architecture-decisions.md) |
| Write tests | `tests/unit/` or `tests/integration/` | [Testing Strategy](docs/context/07-testing-strategy.md) |

---

## Validation Checklist

Before submitting changes:

```bash
uv sync
uv run ruff format src/
uv run ruff check src/
uv run mypy src/                          # budget 75+ seconds
packer fmt -recursive src/bilder/         # if bilder files changed
pulumi preview                            # from inside the project dir
uv run pytest tests/
uv run pre-commit run --all-files         # optional; budget 2+ minutes
```

---

## Key Resources

### Documentation

- **Repository Overview:** `docs/context/01-repository-overview.md`
- **Architecture Decision Records:** `docs/adr/` directory
- **README.md:** High-level project overview
- **Source Code Examples:**
  - Components: `src/ol_infrastructure/components/aws/`
  - Helper utilities: `src/ol_infrastructure/lib/`
  - Packer examples: `src/bilder/images/consul/deploy.py`
  - Test examples: `tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py`

### Configuration Files

- `pyproject.toml` — Dependencies and tool configuration
- `.pre-commit-config.yaml` — Pre-commit hook definitions
- `.sops.yaml` — Secrets encryption rules
- `uv.lock` — Locked dependency versions

### Tools & Versions

- Python 3.12.7
- uv 0.9.3 (use instead of Poetry)
- Pulumi 3.199.0
- Packer 1.14.2
- AWS CLI 2.31.17
- SOPS 3.11.0
- PyInfra 3.5.1

---

## When to Consult Each Guide

| Question | Guide |
|----------|-------|
| Where do I find or add code? | [Repository Overview](docs/context/01-repository-overview.md) |
| How do I run tests or validate code? | [Build and Validation](docs/context/02-build-and-validation.md) |
| How do I deploy infrastructure? | [`src/ol_infrastructure/AGENTS.md`](src/ol_infrastructure/AGENTS.md) |
| How do I build AMIs? | [`src/bilder/AGENTS.md`](src/bilder/AGENTS.md) |
| How do I write a Concourse pipeline? | [`src/ol_concourse/AGENTS.md`](src/ol_concourse/AGENTS.md) |
| How do I manage secrets? | [Secrets Management](docs/context/05-secrets-management.md) |
| What code style should I follow? | [Code Style & Conventions](docs/context/06-code-style-conventions.md) |
| How do I write tests? | [Testing Strategy](docs/context/07-testing-strategy.md) |
| Should I create an ADR? | [Architecture Decisions](docs/context/08-architecture-decisions.md) |
| How do I fix errors? | [Troubleshooting](docs/context/09-troubleshooting.md) |

---

## Common Patterns

### Python Code

```python
# All functions require type hints
def create_resource(name: str, config: dict[str, Any]) -> Resource:
    pass

from pydantic import BaseModel
class Config(BaseModel):
    environment: str
    replicas: int = 3
```

For Pulumi component patterns, stack commands, and test structure with Pulumi mocks,
see [`src/ol_infrastructure/AGENTS.md`](src/ol_infrastructure/AGENTS.md).

---

## Learning Resources

### External

- **Pulumi Documentation:** <https://www.pulumi.com/docs/>
- **Packer Documentation:** <https://www.packer.io/docs>
- **Pytest Documentation:** <https://docs.pytest.org/>

### Internal

- **Test Examples:** `tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py`
- **Component Examples:** `src/ol_infrastructure/components/aws/`
- **ADR Examples:** `docs/adr/0001-*.md`, `docs/adr/0002-*.md`

---

## Critical Notes

1. **Use `uv`, NOT Poetry** — Repository has migrated to uv despite README mentioning Poetry
2. **Expect existing errors** — ~1316 mypy errors and ~809 ruff errors exist; only fix NEW errors
3. **Type hints required** — All functions must have type annotations
4. **Test reusable components** — Always write tests for components in `src/ol_infrastructure/components/`
5. **Create ADRs for major decisions** — Document architectural changes in ADRs
6. **Validate before committing** — Run the validation checklist to catch issues early
