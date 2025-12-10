# Testing Infrastructure

This directory contains tests for the ol-infrastructure Pulumi projects. The test suite validates infrastructure-as-code components, helper libraries, and deployment workflows.

## Directory Structure

```
tests/
├── conftest.py                    # Shared pytest fixtures
├── __init__.py
├── ol_infrastructure/
│   ├── components/                # Component tests (unit tests)
│   │   ├── aws/                   # AWS-specific component tests
│   │   │   └── test_kubernetes_app_auth.py
│   │   ├── alb_fargate_service.py
│   │   ├── database.py
│   │   ├── fargate_service.py
│   │   └── olvpc.py
│   └── lib/                       # Library/helper tests
│       └── ol_types.py
└── README.md                      # This file
```

**Planned structure (not yet created):**

```
tests/
├── unit/                          # Fast unit tests (no deployment)
│   ├── components/
│   └── lib/
├── integration/                   # Slow integration tests (deploy infra)
│   └── test_*.py
└── policies/                      # Policy pack tests
    └── test_*.py
```

## Test Types

### Unit Tests (Current)

Unit tests validate component behavior using Pulumi's mocking system. They run fast (<1s) and don't deploy actual infrastructure.

**Location:** `tests/ol_infrastructure/components/`

**Run:**
```bash
uv run pytest tests/
```

**Example:** `tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py`

### Integration Tests (Planned)

Integration tests deploy actual infrastructure to ephemeral environments, validate behavior, then clean up. These tests are slow but provide end-to-end validation.

**Location:** `tests/integration/` (to be created)

**Run:**
```bash
# Run all integration tests (slow - deploys to AWS)
uv run pytest tests/integration/ -v

# Skip integration tests
uv run pytest tests/ -m "not integration"
```

### Policy Tests (Planned)

Policy tests validate Pulumi Policy Packs against sample infrastructure configurations.

**Location:** `tests/policies/` (to be created)

**Run:**
```bash
uv run pytest tests/policies/ -v
```

## Running Tests

### Basic Commands

```bash
# Run all tests
uv run pytest tests/

# Run with verbose output
uv run pytest tests/ -v

# Run specific test file
uv run pytest tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py

# Run specific test class
uv run pytest tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py::TestOLKubernetesApplicationAuth

# Run specific test method
uv run pytest tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py::TestOLKubernetesApplicationAuth::test_service_account
```

### Advanced Options

```bash
# Run with coverage report
uv run pytest tests/ --cov=src/ol_infrastructure --cov-report=term-missing

# Generate HTML coverage report
uv run pytest tests/ --cov=src/ol_infrastructure --cov-report=html
open htmlcov/index.html

# Run only unit tests (fast)
uv run pytest tests/ -m "unit"

# Skip integration tests (default behavior)
uv run pytest tests/ -m "not integration"

# Run with stdout/logging visible
uv run pytest tests/ -v -s

# Stop on first failure
uv run pytest tests/ -x

# Run last failed tests only
uv run pytest tests/ --lf
```

## Writing Tests

### Unit Test Example

```python
"""Tests for MyComponent."""
import pulumi


class MyMocks(pulumi.runtime.Mocks):
    """Mock implementation for Pulumi resources."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Mock resource creation."""
        outputs = args.inputs
        # Add specific outputs for certain resource types
        if args.typ == "aws:ec2/instance:Instance":
            outputs = {
                **args.inputs,
                "publicIp": "203.0.113.12",
            }
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        """Mock data source calls."""
        if args.token == "aws:ec2/getAmi:getAmi":  # noqa: S105
            return {"id": "ami-12345", "architecture": "x86_64"}
        return {}


# Set mocks BEFORE importing code under test
pulumi.runtime.set_mocks(MyMocks())

# Now import the component
from ol_infrastructure.components.aws.my_component import MyComponent

# Create component at module level
test_component = MyComponent(
    "test-component",
    vpc_id="vpc-12345",
    subnet_ids=["subnet-1", "subnet-2"],
)


# Tests use @pulumi.runtime.test decorator
@pulumi.runtime.test
def test_security_group_created():
    """Verify security group is created."""
    def check_vpc(args):
        urn, vpc_id = args
        assert vpc_id == "vpc-12345", f"Expected vpc-12345, got {vpc_id}"

    return pulumi.Output.all(
        test_component.security_group.urn,
        test_component.security_group.vpc_id
    ).apply(check_vpc)


@pulumi.runtime.test
def test_instance_has_tags():
    """Verify instance has required tags."""
    def check_tags(args):
        urn, tags = args
        assert tags, f"Instance {urn} must have tags"
        assert "Name" in tags, f"Instance {urn} must have Name tag"

    return pulumi.Output.all(
        test_component.instance.urn,
        test_component.instance.tags
    ).apply(check_tags)
```

### Key Testing Patterns

**1. Module-level setup:**
```python
import pulumi

# Define and set mocks at module level
class MyMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):
        return [args.name + "_id", args.inputs]
    def call(self, args):
        return {}

pulumi.runtime.set_mocks(MyMocks())

# Import code AFTER setting mocks
from ol_infrastructure.components.aws.my_component import MyComponent
```

**2. Test functions return Output.apply():**
```python
@pulumi.runtime.test
def test_property():
    def check(args):
        urn, value = args
        assert value == "expected"

    # RETURN the apply() call
    return pulumi.Output.all(
        component.resource.urn,
        component.resource.property
    ).apply(check)
```

**3. Testing multiple outputs:**
```python
@pulumi.runtime.test
def test_multiple_values():
    def check(args):
        urn, prop1, prop2, prop3 = args
        assert prop1 == "value1"
        assert prop2 == 42
        assert prop3 is True

    return pulumi.Output.all(
        component.resource.urn,
        component.resource.property1,
        component.resource.property2,
        component.resource.property3,
    ).apply(check)
```

**4. Testing collections (security group rules):**
```python
@pulumi.runtime.test
def test_no_public_ssh():
    def check_rules(args):
        urn, ingress = args
        ssh_open = any(
            rule["from_port"] == 22
            and "0.0.0.0/0" in rule["cidr_blocks"]
            for rule in ingress
        )
        assert not ssh_open, f"SG {urn} exposes SSH to internet"

    return pulumi.Output.all(
        security_group.urn,
        security_group.ingress
    ).apply(check_rules)
```

## Shared Fixtures

The `conftest.py` file provides shared fixtures available to all tests:

- `pulumi_mocks` — Pulumi mock class with common AWS data sources mocked
- `aws_region` — Default AWS region (`us-east-1`)
- `mock_vpc_id` — Mock VPC ID for tests
- `mock_subnet_ids` — Mock subnet IDs for tests
- `mock_security_group_id` — Mock security group ID
- `mock_tags` — Standard resource tags

**Note:** When using `@pulumi.runtime.test`, you typically don't need these fixtures. The standard pattern is to set mocks at module level before importing your code.

**Usage (if needed for non-Pulumi tests):**
```python
def test_helper_function(aws_region, mock_vpc_id):
    """Test a helper function (not a Pulumi resource)."""
    result = my_helper_function(region=aws_region, vpc_id=mock_vpc_id)
    assert result == "expected"
```

## Mocking AWS Data Sources

When using `@pulumi.runtime.test`, define mocks at module level:

```python
import pulumi

class MyMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):
        return [args.name + "_id", args.inputs]

    def call(self, args):
        # Mock AWS data sources
        if args.token == "aws:ec2/getVpc:getVpc":  # noqa: S105
            return {"id": "vpc-12345", "cidrBlock": "10.0.0.0/16"}
        if args.token == "aws:ec2/getAmi:getAmi":  # noqa: S105
            return {"id": "ami-abc123", "architecture": "x86_64"}
        if args.token == "aws:rds/getInstance:getInstance":  # noqa: S105
            return {"endpoint": "db.example.com:5432"}
        return {}

# Set mocks BEFORE importing infrastructure code
pulumi.runtime.set_mocks(MyMocks())
```

The `conftest.py` file includes a `pulumi_mocks` fixture class with common mocks, but it's not automatically applied when using `@pulumi.runtime.test`.

## Test Markers

Use pytest markers to categorize tests:

```python
import pytest

@pytest.mark.unit
def test_fast_unit_test():
    """Fast test that doesn't deploy infrastructure."""
    pass

@pytest.mark.integration
def test_slow_integration_test():
    """Slow test that deploys to AWS."""
    pass

@pytest.mark.policy
def test_policy_enforcement():
    """Test a Pulumi policy pack."""
    pass
```

**Run tests by marker:**
```bash
# Run only unit tests
uv run pytest tests/ -m "unit"

# Run only integration tests
uv run pytest tests/ -m "integration"

# Skip integration tests
uv run pytest tests/ -m "not integration"
```

## Testing Best Practices

### DO:
- ✅ Write unit tests for all reusable components
- ✅ Use descriptive test names
- ✅ Test both success and failure cases
- ✅ Use fixtures for repeated setup
- ✅ Mock external dependencies
- ✅ Clean up resources in integration tests
- ✅ Test outputs, not implementation

### DON'T:
- ❌ Test private implementation details
- ❌ Skip cleanup in integration tests
- ❌ Hard-code resource IDs
- ❌ Run integration tests on every commit
- ❌ Mix unit and integration tests in same file

## CI/CD Integration

**Pre-commit hooks** (run automatically before commit):
```bash
uv run pre-commit run --all-files
```

**Manual validation before PR:**
```bash
# 1. Run unit tests
uv run pytest tests/ -v

# 2. Run linting
uv run ruff format src/ tests/
uv run ruff check src/ tests/

# 3. Run type checking
uv run mypy src/

# 4. (Optional) Run integration tests
uv run pytest tests/integration/ -v
```

## Troubleshooting

### Common Issues

**1. `pulumi.runtime.Mocks` not working:**
- Ensure `pulumi_mocks` fixture is in `conftest.py`
- Check that fixture is `scope="session"` and `autouse=True`

**2. Tests can't import modules:**
- Ensure `tests/__init__.py` exists
- Run `uv sync` to install dependencies
- Check Python path: `uv run python -c "import sys; print(sys.path)"`

**3. Outputs not resolving in tests:**
- Use `pulumi.Output.all()` to unwrap outputs
- Use `.apply()` with a callback function
- Don't try to access `.value` directly in unit tests

**4. Tests hang or timeout:**
- Check for missing mocks (calls to real AWS APIs)
- Ensure mocks return data for all data sources used
- Use `pytest -v -s` to see where it's hanging

## Additional Resources

- [Pulumi Unit Testing Guide](https://www.pulumi.com/docs/iac/guides/testing/unit/)
- [Pulumi Integration Testing](https://www.pulumi.com/docs/iac/guides/testing/integration/)
- [Pytest Documentation](https://docs.pytest.org/)
- [AGENTS.md Testing Section](../AGENTS.md#testing-pulumi-infrastructure-code)

## Contributing

When adding new tests:

1. Place unit tests in `tests/ol_infrastructure/` mirroring `src/` structure
2. Place integration tests in `tests/integration/`
3. Add fixtures to `conftest.py` if reusable
4. Update this README if adding new test patterns
5. Ensure tests pass before committing:
   ```bash
   uv run pytest tests/ -v
   ```
