# Agent Instructions for ol-infrastructure Repository

This is a **large-scale infrastructure-as-code monorepo** (467 Python files, ~91K lines of code) for managing MIT Open Learning's cloud infrastructure using Pulumi, Packer/PyInfra, and Concourse.

**Trust these instructions.** They are comprehensive and validated. Only search for additional information if these instructions are incomplete or incorrect.

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

### 🔄 [Pulumi Workflows](docs/context/03-pulumi-workflows.md)
- Pulumi project structure (68 projects total)
- Stack naming conventions and management
- Running Pulumi commands (preview, deploy)
- Code style conventions for Pulumi
- File modification tasks and validation

**Use when:** Creating or modifying Pulumi infrastructure, deploying changes

### 📦 [Packer AMI Building](docs/context/04-packer-bilder.md)
- Packer workflow and directory structure
- PyInfra provisioning scripts
- HCL formatting and validation
- Best practices for reusable components

**Use when:** Building or modifying AMIs, creating Packer templates

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
| Add AWS resource | `src/ol_infrastructure/infrastructure/aws/<service>/__main__.py` | [Pulumi Workflows](docs/context/03-pulumi-workflows.md) |
| Create new app | `src/ol_infrastructure/applications/<app>/` | [Pulumi Workflows](docs/context/03-pulumi-workflows.md) |
| Add reusable component | `src/ol_infrastructure/components/<category>/` | [Pulumi Workflows](docs/context/03-pulumi-workflows.md) + [Testing](docs/context/07-testing-strategy.md) |
| Modify AMI build | `src/bilder/images/<image>/deploy.py` | [Packer AMI Building](docs/context/04-packer-bilder.md) |
| Add CI/CD pipeline | `src/ol_concourse/pipelines/<category>/` | [Pulumi Workflows](docs/context/03-pulumi-workflows.md) |
| Manage secrets | `src/bridge/secrets/<context>/` | [Secrets Management](docs/context/05-secrets-management.md) |
| Document architecture | `docs/adr/NNNN-title.md` | [Architecture Decisions](docs/context/08-architecture-decisions.md) |
| Write tests | `tests/unit/` or `tests/integration/` | [Testing Strategy](docs/context/07-testing-strategy.md) |

---

## Validation Checklist

Before submitting changes:

```bash
# 1. Install dependencies
uv sync

# 2. Format code
uv run ruff format src/

# 3. Check code quality
uv run ruff check src/

# 4. Type check (budget 75+ seconds)
uv run mypy src/

# 5. Format Packer files (if modified)
packer fmt -recursive src/bilder/

# 6. Validate Pulumi changes (if applicable)
pulumi preview

# 7. Run tests (if applicable)
uv run pytest tests/

# 8. Optional: Run all pre-commit hooks (budget 2+ minutes)
uv run pre-commit run --all-files
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
| How do I deploy infrastructure? | [Pulumi Workflows](docs/context/03-pulumi-workflows.md) |
| How do I build AMIs? | [Packer AMI Building](docs/context/04-packer-bilder.md) |
| How do I manage secrets? | [Secrets Management](docs/context/05-secrets-management.md) |
| What code style should I follow? | [Code Style & Conventions](docs/context/06-code-style-conventions.md) |
| How do I write tests? | [Testing Strategy](docs/context/07-testing-strategy.md) |
| Should I create an ADR? | [Architecture Decisions](docs/context/08-architecture-decisions.md) |
| How do I fix errors? | [Troubleshooting](docs/context/09-troubleshooting.md) |

---

## Common Patterns

### Python Code

```python
# Type hints (required)
def create_resource(name: str, config: dict[str, Any]) -> Resource:
    """Create resource with config."""
    pass

# Pydantic models
from pydantic import BaseModel
class Config(BaseModel):
    environment: str
    replicas: int = 3
```

### Pulumi Infrastructure

```python
import pulumi
from ol_infrastructure.lib.ol_types import AWSBase

class MyComponent(pulumi.ComponentResource):
    def __init__(self, name: str, config: MyConfig, opts=None):
        super().__init__("custom:resource:MyComponent", name, opts=opts)
        # Create resources and export outputs
```

### Pulumi Commands

```bash
# Preview changes before deploying
cd src/ol_infrastructure/infrastructure/aws/network/
pulumi stack select infrastructure.aws.network.QA
pulumi preview

# Deploy changes
pulumi up
```

### Test Structure

```python
import pulumi
import asyncio

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

# Set mocks BEFORE importing code under test
class MyMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):
        return [args.name + "_id", args.inputs]

pulumi.runtime.set_mocks(MyMocks())
from ol_infrastructure.components.aws.my_component import MyComponent

# Write tests with @pulumi.runtime.test decorator
@pulumi.runtime.test
def test_component():
    def check(args):
        assert args == expected
    return pulumi.Output.all(...).apply(check)
```

---

## Learning Resources

### External
- **Pulumi Documentation:** https://www.pulumi.com/docs/
- **Packer Documentation:** https://www.packer.io/docs
- **Pytest Documentation:** https://docs.pytest.org/

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

---

## Next Steps

1. **Read** the overview docs relevant to your task
2. **Follow** the code style and conventions guide
3. **Validate** your changes using the checklist
4. **Submit** your code with tests and documentation

For specific tasks, consult the appropriate guide in the [Documentation Index](#documentation-index).
