# Testing Strategy for Pulumi Infrastructure

## Testing Philosophy

Testing Pulumi infrastructure code is critical for ensuring reliability and preventing configuration drift. This repository supports three types of testing:

1. **Unit Testing** — Test individual components and resources in isolation using mocked dependencies
2. **Property/Policy Testing** — Validate resource compliance against organizational standards
3. **Integration Testing** — Deploy to ephemeral environments and validate end-to-end behavior

**Current state:** Repository has minimal test coverage (~1 unit test example). Expand test coverage as you develop.

## When to Write Tests

### ALWAYS write tests when you:

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

### CONSIDER writing tests when you:
- Create new Pulumi projects (applications or infrastructure)
- Implement complex IAM policies or security group rules
- Build multi-resource orchestration logic

### SKIP tests for:
- Simple stack configurations (mostly passing values)
- One-off infrastructure changes
- Prototypes or experiments
- Changes solely to documentation

## Unit Testing with Pulumi Mocks

**What are unit tests?** Tests that validate resource properties and relationships without deploying infrastructure. Use Pulumi's mocking system to intercept resource creation.

### Unit Test Structure

**Python 3.14+ Compatibility Note:** Add event loop setup at the top of test files:

```python
import asyncio

# Ensure event loop exists for Python 3.14+
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
```

### Basic Test Example

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
        """Mock resource creation."""
        outputs = args.inputs
        if args.typ == "aws:ec2/instance:Instance":
            outputs = {
                **args.inputs,
                "publicIp": "203.0.113.12",
                "publicDns": "ec2-203-0-113-12.compute-1.amazonaws.com",
            }
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        """Mock data source calls."""
        if args.token == "aws:ec2/getAmi:getAmi":
            return {
                "architecture": "x86_64",
                "id": "ami-0eb1f3cdeeb8eed2a",
            }
        return {}


# Set mocks BEFORE importing code under test
pulumi.runtime.set_mocks(MyMocks())

# Import after mocks are set
from ol_infrastructure.components.aws.my_component import MyComponent


# Create component at module level
test_component = MyComponent(
    "test-component",
    vpc_id="vpc-12345",
    subnet_ids=["subnet-1", "subnet-2"],
)


@pulumi.runtime.test
def test_security_group_created():
    """Verify security group is created with correct VPC."""
    def check_vpc_id(args):
        urn, vpc_id = args
        assert vpc_id == "vpc-12345"

    return pulumi.Output.all(
        test_component.security_group.urn,
        test_component.security_group.vpc_id
    ).apply(check_vpc_id)


@pulumi.runtime.test
def test_tags_applied():
    """Verify required tags are present."""
    def check_tags(args):
        urn, tags = args
        assert tags
        assert "Environment" in tags

    return pulumi.Output.all(
        test_component.instance.urn,
        test_component.instance.tags
    ).apply(check_tags)
```

### Key Testing Patterns

**1. Module-level setup (CRITICAL):**
```python
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
        assert actual_value == "expected"

    # RETURN the Output.apply() call
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
        assert urn
        assert "my-component" in urn

    return component.my_resource.urn.apply(check_exists)
```

**5. Mocking AWS data sources:**
```python
class MyMocks(pulumi.runtime.Mocks):
    def call(self, args):
        if args.token == "aws:ec2/getAmi:getAmi":
            return {
                "architecture": "x86_64",
                "id": "ami-0eb1f3cdeeb8eed2a",
            }
        if args.token == "aws:ec2/getVpc:getVpc":
            return {
                "id": "vpc-12345678",
                "cidrBlock": "10.0.0.0/16",
            }
        return {}
```

### Running Unit Tests

```bash
# Run all unit tests
uv run pytest tests/

# Run specific test file
uv run pytest tests/unit/components/test_my_component.py

# Run with verbose output
uv run pytest tests/ -v

# Run specific test function
uv run pytest tests/test_my_component.py::test_security_group_created

# Disable pytest warnings (Pulumi generates many deprecation warnings)
uv run pytest tests/ --disable-pytest-warnings
```

## Integration Testing

**When to use:** Validate multi-resource orchestration, deployment lifecycle, runtime behavior.

### Automation API Approach (Recommended)

```python
"""Integration tests for network infrastructure."""
import pytest
import pulumi
from pulumi import automation as auto


@pytest.mark.integration
def test_network_stack_lifecycle():
    """Test deploying and destroying network stack."""
    project_name = "network-test"
    stack_name = "test"
    work_dir = "src/ol_infrastructure/infrastructure/aws/network"

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

    finally:
        # Always clean up
        stack.destroy(on_output=print)
        stack.workspace.remove_stack(stack_name)
```

### Running Integration Tests

```bash
# Run all integration tests (slow - deploys infrastructure)
uv run pytest tests/integration/ -v

# Run specific integration test
uv run pytest tests/integration/test_network.py::test_network_stack_lifecycle

# Skip integration tests (run only unit tests)
uv run pytest tests/ -m "not integration"
```

## Test Organization

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

## Testing Best Practices

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

## Testing Checklist for AI Agents

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

3. **Documentation:**
   - [ ] Added docstrings to test functions
   - [ ] Updated test README if needed
   - [ ] Documented any special test setup requirements

## Example Test Workflow

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

## Testing Resources

- **Pulumi Unit Testing:** https://www.pulumi.com/docs/iac/guides/testing/unit/
- **Pulumi Integration Testing:** https://www.pulumi.com/docs/iac/guides/testing/integration/
- **Automation API:** https://www.pulumi.com/docs/iac/packages-and-automation/automation-api/
- **Pytest Documentation:** https://docs.pytest.org/
- **Example Repository:** `tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py`
