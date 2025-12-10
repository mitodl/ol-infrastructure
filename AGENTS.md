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
# Run all tests (currently minimal test coverage)
uv run pytest tests/

# Run tests with verbose output
uv run pytest tests/ -v

# Run specific test file
uv run pytest tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py

# Run tests with coverage report
uv run pytest tests/ --cov=src/ol_infrastructure --cov-report=term-missing
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
| Add reusable component | `src/ol_infrastructure/components/<category>/` | Used in other projects + unit tests |
| Modify AMI build | `src/bilder/images/<image>/deploy.py` or `.pkr.hcl` | Packer build (CI only) |
| Add CI/CD pipeline | `src/ol_concourse/pipelines/<category>/` | Generate YAML, deploy to Concourse |
| Add secret | Encrypt with `sops`, place in `src/bridge/secrets/<context>/`, decrypt in Pulumi | `sops` CLI |
| Document architecture decision | Create `docs/adr/NNNN-title.md` using template | ADR format validation |
| Add component unit test | Create `tests/unit/components/test_<name>.py` with PulumiMocks | `uv run pytest tests/unit/` |
| Add integration test | Create `tests/integration/test_<name>.py` using Automation API | `uv run pytest tests/integration/ -v` |
| Add policy pack | Create `policy-packs/<name>/` with `__main__.py` | `pulumi preview --policy-pack` |

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
- `docs/adr/` — Architecture Decision Records (see ADR section below)

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

---

## Testing Pulumi Infrastructure Code

### Testing Philosophy

Testing Pulumi infrastructure code is critical for ensuring reliability and preventing configuration drift. This repository supports three types of testing:

1. **Unit Testing** — Test individual components and resources in isolation using mocked dependencies
2. **Property/Policy Testing** — Validate resource compliance against organizational standards
3. **Integration Testing** — Deploy to ephemeral environments and validate end-to-end behavior

**Current state:** Repository has minimal test coverage (~1 unit test example). Expand test coverage as you develop.

### When to Write Tests (for AI Agents)

**ALWAYS write tests when you:**

1. **Create new reusable components** (`src/ol_infrastructure/components/`)
   - ✅ Test component initialization with various configurations
   - ✅ Validate resource properties match inputs
   - ✅ Verify dependent resources are created correctly

2. **Add complex resource logic** (conditional creation, computed properties)
   - ✅ Test all code paths and edge cases
   - ✅ Validate property transformations
   - ✅ Test error handling

3. **Modify existing components with tests**
   - ✅ Update existing tests to reflect new behavior
   - ✅ Add tests for new functionality

4. **Create helper functions** (`src/ol_infrastructure/lib/`)
   - ✅ Test pure functions with various inputs
   - ✅ Validate edge cases and error conditions

**CONSIDER writing tests when you:**

- Create new Pulumi projects (applications or infrastructure)
- Implement complex IAM policies or security group rules
- Build multi-resource orchestration logic

**SKIP tests for:**

- Simple stack configurations (mostly passing values)
- One-off infrastructure changes
- Prototypes or experiments
- Changes solely to documentation

### Unit Testing with Pulumi Mocks

**What are unit tests?** Tests that validate resource properties and relationships without deploying infrastructure. Use Pulumi's mocking system to intercept resource creation.

**Example location:** `tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py`

#### Unit Test Structure

**Python 3.14+ Compatibility Note:** Pulumi's `set_mocks()` requires an event loop. Add this at the top of test files:

```python
import asyncio

# Ensure event loop exists for Python 3.14+
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
```

```python
"""Tests for MyComponent."""
import asyncio
import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())


class MyMocks(pulumi.runtime.Mocks):
    """Mock implementation for Pulumi resources."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Mock resource creation - return resource ID and outputs."""
        outputs = args.inputs
        # Add mock outputs for specific resource types
        if args.typ == "aws:ec2/instance:Instance":
            outputs = {
                **args.inputs,
                "publicIp": "203.0.113.12",
                "publicDns": "ec2-203-0-113-12.compute-1.amazonaws.com",
            }
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        """Mock data source calls."""
        if args.token == "aws:ec2/getAmi:getAmi":  # noqa: S105
            return {
                "architecture": "x86_64",
                "id": "ami-0eb1f3cdeeb8eed2a",
            }
        return {}


# Set mocks BEFORE importing code under test
pulumi.runtime.set_mocks(MyMocks())

# Now import the component/infrastructure code
from ol_infrastructure.components.aws.my_component import MyComponent


# Create the component at module level or in a fixture
test_component = MyComponent(
    "test-component",
    vpc_id="vpc-12345",
    subnet_ids=["subnet-1", "subnet-2"],
)


# Tests use @pulumi.runtime.test decorator and RETURN Output.apply()
@pulumi.runtime.test
def test_security_group_created():
    """Verify security group is created with correct VPC."""
    def check_vpc_id(args):
        urn, vpc_id = args
        assert vpc_id == "vpc-12345", f"Expected vpc-12345, got {vpc_id}"

    return pulumi.Output.all(
        test_component.security_group.urn,
        test_component.security_group.vpc_id
    ).apply(check_vpc_id)


@pulumi.runtime.test
def test_instance_type():
    """Verify instance uses correct instance type."""
    def check_instance_type(args):
        urn, instance_type = args
        assert instance_type == "t3.micro", f"Expected t3.micro, got {instance_type}"

    return pulumi.Output.all(
        test_component.instance.urn,
        test_component.instance.instance_type
    ).apply(check_instance_type)


@pulumi.runtime.test
def test_tags_applied():
    """Verify required tags are present."""
    def check_tags(args):
        urn, tags = args
        assert tags, f"Resource {urn} must have tags"
        assert "Environment" in tags, f"Resource {urn} must have Environment tag"

    return pulumi.Output.all(
        test_component.instance.urn,
        test_component.instance.tags
    ).apply(check_tags)
```

**See working example:** `tests/test_example_correct_pattern.py`

#### Key Unit Testing Patterns

**1. Module-level setup (CRITICAL):**
```python
import pulumi

# Define mocks at module level
class MyMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):
        return [args.name + "_id", args.inputs]

    def call(self, args):
        return {}

# Set mocks BEFORE importing code under test
pulumi.runtime.set_mocks(MyMocks())

# NOW import your infrastructure code
from ol_infrastructure.components.aws.my_component import MyComponent
```

**2. Use @pulumi.runtime.test decorator:**
```python
@pulumi.runtime.test
def test_resource_property():
    """Test description."""
    def check_value(args):
        urn, actual_value = args
        assert actual_value == "expected", f"Resource {urn} has wrong value"

    # RETURN the Output.apply() call (don't just call it)
    return pulumi.Output.all(
        component.resource.urn,
        component.resource.property
    ).apply(check_value)
```

**3. Testing multiple outputs:**
```python
@pulumi.runtime.test
def test_multiple_properties():
    def check_values(args):
        urn, vpc_id, instance_type, tags = args
        assert vpc_id == "vpc-12345"
        assert instance_type == "t3.micro"
        assert "Name" in tags

    return pulumi.Output.all(
        component.instance.urn,
        component.instance.vpc_id,
        component.instance.instance_type,
        component.instance.tags,
    ).apply(check_values)
```

**4. Testing resource existence:**
```python
@pulumi.runtime.test
def test_resource_created():
    """Verify resource was created."""
    def check_exists(urn):
        # If we got a URN, resource exists
        assert urn, "Resource was not created"
        assert "my-component" in urn

    return component.my_resource.urn.apply(check_exists)
```

**5. Testing security group rules:**
```python
@pulumi.runtime.test
def test_no_ssh_from_internet():
    """Ensure SSH is not open to the internet."""
    def check_ingress_rules(args):
        urn, ingress = args
        ssh_open = any(
            rule["from_port"] == 22
            and any(block == "0.0.0.0/0" for block in rule["cidr_blocks"])
            for rule in ingress
        )
        assert not ssh_open, f"Security group {urn} exposes SSH to internet"

    return pulumi.Output.all(
        security_group.urn,
        security_group.ingress
    ).apply(check_ingress_rules)
```

**6. Mocking AWS data sources:**
```python
class MyMocks(pulumi.runtime.Mocks):
    def call(self, args):
        # Mock AWS AMI lookup
        if args.token == "aws:ec2/getAmi:getAmi":  # noqa: S105
            return {
                "architecture": "x86_64",
                "id": "ami-0eb1f3cdeeb8eed2a",
            }
        # Mock VPC lookup
        if args.token == "aws:ec2/getVpc:getVpc":  # noqa: S105
            return {
                "id": "vpc-12345678",
                "cidrBlock": "10.0.0.0/16",
            }
        return {}
```

#### Running Unit Tests

```bash
# Run all unit tests
uv run pytest tests/

# Run specific test file
uv run pytest tests/ol_infrastructure/components/test_my_component.py

# Run with verbose output
uv run pytest tests/ -v

# Run specific test function
uv run pytest tests/test_my_component.py::test_security_group_created

# Disable pytest warnings (Pulumi generates many deprecation warnings)
uv run pytest tests/ --disable-pytest-warnings
```

**Important:** When using `@pulumi.runtime.test`, you don't need pytest classes or fixtures. Tests are simple functions at module level.

### Property/Policy Testing

**What is policy testing?** Validates that infrastructure resources comply with organizational standards and security requirements. Unlike unit tests that check correctness, policy tests enforce governance.

**Policy testing in this repository:**
- Policies are written as Pulumi Policy Packs (TypeScript or Python)
- Policies validate resource properties during `pulumi preview` and `pulumi up`
- Can enforce mandatory, advisory, or warning-level rules

**When to write policies:**
- Enforce security standards (encryption, network isolation)
- Ensure cost controls (instance types, resource limits)
- Validate compliance requirements (tagging, naming conventions)
- Prevent misconfigurations (public S3 buckets, open security groups)

**Policy Pack Structure:**
```
policy-packs/
├── aws-security/              # Example policy pack
│   ├── PulumiPolicy.yaml      # Policy pack metadata (Python)
│   ├── __main__.py            # Policy definitions (Python)
│   └── requirements.txt       # Dependencies
```

**Example Policy (Python):**
```python
from pulumi_policy import (
    EnforcementLevel,
    PolicyPack,
    ResourceValidationPolicy,
)

def s3_bucket_encryption_validator(args, report_violation):
    """Ensure S3 buckets have encryption enabled."""
    if args.resource_type == "aws:s3/bucket:Bucket":
        encryption = args.props.get("serverSideEncryptionConfiguration")
        if not encryption:
            report_violation("S3 bucket must have server-side encryption enabled.")

s3_encryption_policy = ResourceValidationPolicy(
    name="s3-bucket-encryption",
    description="Validates that S3 buckets have encryption enabled.",
    validate=s3_bucket_encryption_validator,
    enforcement_level=EnforcementLevel.MANDATORY,
)

PolicyPack(
    name="aws-security",
    enforcement_level=EnforcementLevel.MANDATORY,
    policies=[s3_encryption_policy],
)
```

**Testing Policies Locally:**
```bash
# Navigate to Pulumi project
cd src/ol_infrastructure/applications/my_app/

# Run policy pack locally (does not enforce, just validates)
pulumi preview --policy-pack ../../../policy-packs/aws-security/
```

**Publishing Policies (requires Pulumi Cloud):**
```bash
cd policy-packs/aws-security/
pulumi policy publish <org-name>
```

**Reference:** See [Pulumi Policy Authoring Guide](https://www.pulumi.com/docs/insights/policy/policy-packs/authoring/) for details.

### Integration Testing

**What is integration testing?** Deploys actual infrastructure to ephemeral environments, validates behavior, then tears down. Tests the complete deployment lifecycle.

**Integration testing options:**

1. **Pulumi Integration Testing Framework (Go)** — Purpose-built framework for testing Pulumi programs
2. **Pulumi Automation API (Python/Node/Go/.NET/Java)** — Programmatic control over Pulumi lifecycle
3. **CLI-based testing (Shell scripts)** — Direct use of `pulumi` commands

**When to use integration tests:**
- Validate multi-resource orchestration
- Test deployment/update/destroy lifecycle
- Verify runtime behavior (HTTP endpoints, database connections)
- Test upgrade paths between versions

#### Option 1: Integration Testing Framework (Go)

**Not currently set up in this repository.** Requires Go test harness.

**Setup required:**
1. Create `tests/integration/` directory
2. Add Go module: `go mod init github.com/mitodl/ol-infrastructure/tests/integration`
3. Add dependency: `go get github.com/pulumi/pulumi/sdk/v3/go/auto`
4. Write tests using `integration.ProgramTest`

**Example structure:**
```go
// tests/integration/network_test.go
package test

import (
    "testing"
    "github.com/pulumi/pulumi/sdk/v3/go/common/resource"
    "github.com/pulumi/pulumi/pkg/v3/testing/integration"
)

func TestNetworkInfrastructure(t *testing.T) {
    integration.ProgramTest(t, &integration.ProgramTestOptions{
        Dir: "../../src/ol_infrastructure/infrastructure/aws/network",
        Quick: true,
        ExtraRuntimeValidation: func(t *testing.T, stack integration.RuntimeValidationStackInfo) {
            // Validate outputs
            vpcId := stack.Outputs["vpc_id"].(string)
            assert.NotEmpty(t, vpcId)
        },
    })
}
```

**Reference:** [Pulumi Integration Testing Framework](https://www.pulumi.com/docs/iac/guides/testing/integration/framework/)

#### Option 2: Automation API (Python)

**Current recommendation for this repository** — Write integration tests in Python using Automation API.

**Setup required:**
1. Add test dependencies to `pyproject.toml`:
   ```toml
   [dependency-groups]
   dev = [
       # ... existing deps
       "pytest-asyncio>=0.24.0",  # For async test support
   ]
   ```

2. Create integration test directory: `tests/integration/`

3. Create test using Automation API:

```python
"""Integration tests for network infrastructure."""
import pytest
import pulumi
from pulumi import automation as auto
import httpx


@pytest.mark.integration  # Mark as integration test (slow, optional)
def test_network_stack_lifecycle():
    """Test deploying and destroying network stack."""
    project_name = "network-test"
    stack_name = "test"
    work_dir = "src/ol_infrastructure/infrastructure/aws/network"

    # Create or select stack
    stack = auto.create_or_select_stack(
        stack_name=stack_name,
        project_name=project_name,
        work_dir=work_dir,
    )

    # Set configuration
    stack.set_config("aws:region", auto.ConfigValue(value="us-east-1"))

    try:
        # Deploy stack
        up_result = stack.up(on_output=print)

        # Validate outputs
        outputs = up_result.outputs
        assert "vpc_id" in outputs
        vpc_id = outputs["vpc_id"].value
        assert vpc_id.startswith("vpc-")

        # Optionally validate runtime behavior
        # Example: Check if resource exists via AWS SDK

    finally:
        # Always clean up
        stack.destroy(on_output=print)
        stack.workspace.remove_stack(stack_name)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_web_endpoint_available():
    """Test that deployed web application responds to HTTP requests."""
    # Deploy stack (abbreviated)
    stack = auto.create_or_select_stack(...)
    up_result = stack.up(on_output=print)

    try:
        # Get endpoint URL from outputs
        endpoint_url = up_result.outputs["endpoint_url"].value

        # Validate HTTP endpoint
        async with httpx.AsyncClient() as client:
            response = await client.get(endpoint_url)
            assert response.status_code == 200
            assert "Welcome" in response.text
    finally:
        stack.destroy(on_output=print)
        stack.workspace.remove_stack(stack_name)
```

**Running Integration Tests:**
```bash
# Run all integration tests (slow - deploys infrastructure)
uv run pytest tests/integration/ -v

# Run specific integration test
uv run pytest tests/integration/test_network.py::test_network_stack_lifecycle

# Skip integration tests (run only unit tests)
uv run pytest tests/ -m "not integration"
```

**Reference:** [Automation API Testing](https://www.pulumi.com/docs/iac/guides/testing/integration/automation-api/)

#### Option 3: CLI-Based Integration Testing

**Simplest approach** — Use shell scripts or Python subprocess calls to run `pulumi` commands.

```python
"""CLI-based integration test."""
import subprocess
import json


def test_stack_deploys_successfully():
    """Test stack deployment via CLI."""
    project_dir = "src/ol_infrastructure/infrastructure/aws/network"
    stack_name = "test"

    # Initialize stack
    subprocess.run(
        ["pulumi", "stack", "init", stack_name],
        cwd=project_dir,
        check=True,
    )

    try:
        # Deploy
        result = subprocess.run(
            ["pulumi", "up", "--yes", "--skip-preview"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        assert b"error" not in result.stderr.lower()

        # Export outputs
        outputs_result = subprocess.run(
            ["pulumi", "stack", "output", "--json"],
            cwd=project_dir,
            capture_output=True,
            check=True,
        )
        outputs = json.loads(outputs_result.stdout)
        assert "vpc_id" in outputs

    finally:
        # Destroy
        subprocess.run(
            ["pulumi", "destroy", "--yes"],
            cwd=project_dir,
        )
        subprocess.run(
            ["pulumi", "stack", "rm", stack_name, "--yes"],
            cwd=project_dir,
        )
```

### Test Organization

**Recommended directory structure:**

```
tests/
├── __init__.py
├── conftest.py                    # Shared pytest fixtures
├── unit/                          # Fast unit tests (no deployment)
│   ├── __init__.py
│   ├── components/
│   │   ├── test_fargate_service.py
│   │   └── test_kubernetes_auth.py
│   └── lib/
│       ├── test_aws_helpers.py
│       └── test_pulumi_helpers.py
├── integration/                   # Slow integration tests (deploy infra)
│   ├── __init__.py
│   ├── test_network_stack.py
│   └── test_application_deployment.py
└── policies/                      # Policy pack tests
    └── test_aws_security_policies.py
```

**Shared fixtures (`conftest.py`):**
```python
"""Shared pytest fixtures for Pulumi tests."""
import pytest
import pulumi


@pytest.fixture(scope="session", autouse=True)
def pulumi_mocks():
    """Set up Pulumi mocks for all unit tests."""
    class PulumiMocks(pulumi.runtime.Mocks):
        def new_resource(self, args: pulumi.runtime.MockResourceArgs):
            return [args.name + "_id", args.inputs]

        def call(self, args: pulumi.runtime.MockCallArgs):
            return {}

    pulumi.runtime.set_mocks(PulumiMocks())


@pytest.fixture
def aws_region():
    """Default AWS region for tests."""
    return "us-east-1"


@pytest.fixture
def mock_vpc_id():
    """Mock VPC ID for tests."""
    return "vpc-12345678"
```

### Test Harness Setup Tasks

**To enable full testing capabilities in this repository:**

1. **Add test dependencies:**
   ```bash
   # Edit pyproject.toml to add:
   # [dependency-groups]
   # dev = [
   #     "pytest-asyncio>=0.24.0",
   #     "pytest-cov>=6.0.0",
   #     "pytest-mock>=3.14.0",
   # ]
   uv sync
   ```

2. **Create test directory structure:**
   ```bash
   mkdir -p tests/unit/components tests/unit/lib tests/integration tests/policies
   touch tests/conftest.py
   ```

3. **Add pytest configuration to `pyproject.toml`:**
   ```toml
   [tool.pytest.ini_options]
   testpaths = ["tests"]
   python_files = ["test_*.py"]
   python_classes = ["Test*"]
   python_functions = ["test_*"]
   markers = [
       "unit: Unit tests (fast, no external dependencies)",
       "integration: Integration tests (slow, deploys infrastructure)",
       "policy: Policy pack tests",
   ]
   # Ignore slow integration tests by default
   addopts = "-v -m 'not integration'"
   ```

4. **Create shared mocks (`tests/conftest.py`):** See example above

5. **Add coverage reporting:**
   ```bash
   uv run pytest tests/ --cov=src/ol_infrastructure --cov-report=html
   open htmlcov/index.html  # View coverage report
   ```

6. **For Go integration tests (optional):**
   ```bash
   mkdir -p tests/integration_go
   cd tests/integration_go
   go mod init github.com/mitodl/ol-infrastructure/tests/integration_go
   go get github.com/pulumi/pulumi/sdk/v3/go/auto
   go get github.com/stretchr/testify/assert
   ```

### Testing Best Practices

**DO:**
- ✅ Write unit tests for all reusable components
- ✅ Use descriptive test names: `test_security_group_blocks_public_ssh`
- ✅ Test both success and failure cases
- ✅ Use `pytest.mark.integration` for slow tests
- ✅ Mock external dependencies (AWS API calls)
- ✅ Clean up resources in integration tests (`try/finally` blocks)
- ✅ Test outputs and resource properties, not implementation details
- ✅ Use fixtures for repeated setup code

**DON'T:**
- ❌ Test implementation details (how resources are created)
- ❌ Skip cleanup in integration tests (avoid resource leaks)
- ❌ Hard-code resource IDs or ARNs in tests
- ❌ Run integration tests on every commit (too slow)
- ❌ Mix unit and integration tests in same file
- ❌ Forget to add new test files to git

### Testing Checklist for AI Agents

When creating or modifying Pulumi code:

1. **Unit tests:**
   - [ ] Created `tests/unit/components/test_<component_name>.py`
   - [ ] Added `PulumiMocks` fixture
   - [ ] Tested resource creation with valid inputs
   - [ ] Tested edge cases and error conditions
   - [ ] Validated resource properties using `.apply()`
   - [ ] Tests pass: `uv run pytest tests/unit/`

2. **Integration tests (if applicable):**
   - [ ] Created `tests/integration/test_<stack_name>.py`
   - [ ] Test deploys stack successfully
   - [ ] Test validates outputs
   - [ ] Test destroys stack in `finally` block
   - [ ] Marked with `@pytest.mark.integration`

3. **Policy tests (if creating policies):**
   - [ ] Created policy pack in `policy-packs/<pack_name>/`
   - [ ] Tested policy with `pulumi preview --policy-pack`
   - [ ] Documented policy in `policy-packs/README.md`

4. **Documentation:**
   - [ ] Added docstrings to test functions
   - [ ] Updated test README if needed
   - [ ] Documented any special test setup requirements

### Example Test Workflow

**When creating a new component:**

1. Create component file: `src/ol_infrastructure/components/aws/my_component.py`
2. Create test file: `tests/unit/components/test_my_component.py`
3. Write component code and tests in parallel (TDD approach)
4. Run tests: `uv run pytest tests/unit/components/test_my_component.py -v`
5. Fix failures, iterate
6. Add integration test if component requires runtime validation
7. Update documentation

**Before submitting PR:**

```bash
# Run all unit tests
uv run pytest tests/unit/ -v

# Run linting
uv run ruff format src/ tests/
uv run ruff check src/ tests/

# Run type checking
uv run mypy src/

# Optionally run integration tests (slow)
uv run pytest tests/integration/ -v
```

### Testing Resources

- **Pulumi Unit Testing:** https://www.pulumi.com/docs/iac/guides/testing/unit/
- **Pulumi Policy Testing:** https://www.pulumi.com/docs/insights/policy/policy-packs/authoring/
- **Pulumi Integration Testing:** https://www.pulumi.com/docs/iac/guides/testing/integration/
- **Automation API:** https://www.pulumi.com/docs/iac/packages-and-automation/automation-api/
- **Pytest Documentation:** https://docs.pytest.org/
- **Example Repository:** `tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py`

---

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

---

## Architecture Decision Records (ADR)

### What are ADRs?

**Architecture Decision Records** (ADRs) document important architectural decisions along with their context and consequences. They capture the "why" behind decisions, not just the "what."

**Key characteristics:**
- Lightweight markdown documents in `docs/adr/`
- Numbered sequentially (`NNNN-title-with-dashes.md`)
- Immutable once accepted (supersede with new ADRs, don't edit)
- Include: Status, Context, Decision, Consequences

### When to Create an ADR (for AI Agents)

**ALWAYS create an ADR when you:**

1. **Make infrastructure architecture changes:**
   - ✅ Change ingress controllers, service mesh, or routing patterns
   - ✅ Introduce new core technologies (databases, caching, messaging)
   - ✅ Modify deployment strategies or blue-green patterns
   - ✅ Change monitoring, logging, or observability approaches
   - ✅ Alter authentication or security mechanisms

2. **Make decisions affecting multiple systems:**
   - ✅ Changes impacting 5+ applications or stacks
   - ✅ Cross-team coordination required
   - ✅ Breaking changes to existing patterns

3. **Evaluate multiple options:**
   - ✅ You compared 2+ approaches during the session
   - ✅ Trade-offs were analyzed
   - ✅ The decision isn't obvious or standard practice

4. **Create significant technical debt or constraints:**
   - ✅ Decision limits future options
   - ✅ Temporary workarounds that will persist
   - ✅ Compromises made due to time/resource constraints

5. **Spend significant effort (>8 hours):**
   - ✅ Large refactoring or migration projects
   - ✅ Multi-phase implementations
   - ✅ Changes that are difficult or expensive to reverse

### When NOT to Create an ADR

**Skip ADRs for:**
- ❌ Trivial changes following established patterns
- ❌ Bug fixes that don't change architecture
- ❌ Code refactoring without architectural impact
- ❌ Configuration value changes (non-architectural)
- ❌ Single-file or single-function changes
- ❌ Obvious choices with no alternatives
- ❌ Temporary experiments or POCs that won't persist

**Rule of thumb:** If a developer 6 months from now would ask "Why did we do it this way?", create an ADR.

### ADR Creation Process for Agents

When you determine an ADR is needed:

1. **During your coding session:**
   ```bash
   # Copy the template
   cp docs/adr/template.md docs/adr/NNNN-your-title.md
   # NNNN = next sequential number (check existing ADRs)
   ```

2. **Fill in the ADR sections:**
   - **Status:** Always start with "Proposed"
   - **Context:** Explain the problem, drivers, constraints, options considered
   - **Decision:** State the chosen option and rationale
   - **Consequences:** List positive, negative, and neutral outcomes

3. **Reference your analysis:**
   - Link to comparison tables or analysis docs you created
   - Include effort estimates and risk assessments
   - Document alternatives you explored

4. **Mark for human review:**
   - Leave status as "Proposed"
   - Add note in "Review History" section: "Created by AI agent, pending human approval"
   - Human will update to "Accepted" or "Rejected"

### ADR Template Quick Reference

```markdown
# NNNN. {Title}

**Status:** Proposed
**Date:** {YYYY-MM-DD}
**Deciders:** {AI Agent + Human Reviewer}
**Technical Story:** {Link to PR/Issue}

## Context
{Problem statement, drivers, constraints, options considered}

## Decision
{Chosen option and rationale}

## Consequences
{Positive, negative, and neutral outcomes}
```

**Full template:** `docs/adr/template.md`
**ADR Guide:** `docs/adr/README.md`

### Examples from This Repository

- **ADR-0001:** Use ADR for Architecture Decisions (meta-ADR)
- **ADR-0002:** Migrate to Gateway API HTTPRoute (example from agentic session)

### ADR Best Practices for Agents

**DO:**
- ✅ Create ADR during the session, not after
- ✅ Document all options you evaluated (not just the chosen one)
- ✅ Be honest about negative consequences and trade-offs
- ✅ Include effort estimates and risk levels
- ✅ Link to related planning docs or analysis
- ✅ Write clearly for future developers (assume they lack your context)

**DON'T:**
- ❌ Mark ADR as "Accepted" (only humans can accept)
- ❌ Edit existing ADRs (create new ADR to supersede)
- ❌ Write ADRs for trivial or standard changes
- ❌ Skip ADRs for architectural decisions (err on side of documenting)
- ❌ Forget to update `docs/adr/README.md` index

### Integration with Workflow

**When making Pulumi changes:**
1. Determine if ADR needed (see criteria above)
2. If yes, create ADR alongside code changes
3. Include ADR in same PR as code changes
4. Human reviews both code and ADR
5. ADR status updated during PR merge

**When exploring options:**
- If you create comparison tables or analysis docs during a session
- AND you make a recommendation
- THEN create an ADR documenting the decision

**When migrating or refactoring:**
- Multi-phase projects (like Gateway API migration) → Create ADR
- Include link to detailed migration plan
- ADR captures the "why" and high-level approach
- Plan captures the "how" and detailed steps

### ADR Numbering

Check existing ADRs to determine next number:
```bash
ls docs/adr/ | grep -E "^[0-9]{4}" | sort -n | tail -1
# Increment the number for your ADR
```

Current sequence: 0002 (use 0003 for next ADR)

### Questions?

- **"Is this architectural?"** If it affects how systems connect, deploy, or operate → Yes
- **"Do I need approval?"** ADRs need human review; mark as "Proposed" and let humans accept
- **"Can I skip this?"** When in doubt, create ADR. 15 minutes now saves hours of confusion later
- **"Where do I learn more?"** Read `docs/adr/README.md` and existing ADRs as examples

---

## When to Search vs. Trust Instructions
