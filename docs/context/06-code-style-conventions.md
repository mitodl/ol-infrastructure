# Code Style & Conventions

## Python Code Style

### Type Hints (Required)
- **All functions/methods must have type annotations**
- Use `typing` module for complex types
- Use `|` operator for unions in Python 3.10+
- Example:
  ```python
  def process_data(items: list[str], timeout: int = 30) -> dict[str, Any]:
      pass
  ```

### Pydantic Models
- Use `BaseModel` for data structures
- Use `BaseSettings` for configuration with environment variable support
- Always inherit from `AWSBase` for AWS-specific configurations
- Example:
  ```python
  from pydantic import BaseModel

  class AppConfig(BaseModel):
      environment: str
      replicas: int = 3
      tags: dict[str, str] = {}
  ```

### Imports
- Follow `ruff` isort rules (run `ruff format` to auto-fix)
- Order: standard library, third-party, local imports
- Organize with blank lines between groups

### Docstrings
- PEP 257 style (enforced by ruff)
- Include type information in docstrings for clarity
- Example:
  ```python
  def create_resource(name: str, config: dict) -> Resource:
      """Create a resource with given configuration.

      Args:
          name: Unique resource identifier
          config: Configuration dictionary

      Returns:
          Created resource instance
      """
  ```

### Formatting
- Run `uv run ruff format src/` before committing
- Line length: Default ruff setting (88 characters)
- Indentation: 4 spaces

## Pulumi Code Style

### Projects
- Each `Pulumi.yaml` directory is a project
- Keep projects focused on a single concern (e.g., networking, database)

### Stacks
- Named as `<namespace>.<environment>` (e.g., `infrastructure.aws.network.QA`)
- Supported environments: `QA`, `Production`, `CI`, `Dev`

### Components
- Inherit from `pulumi.ComponentResource`
- Accept configuration via Pydantic models
- Export outputs as class attributes
- Include comprehensive docstrings
- Example structure:
  ```python
  from pulumi import ComponentResource, export

  class MyComponent(ComponentResource):
      def __init__(self, name: str, config: MyConfig, opts=None):
          super().__init__("custom:resource:MyComponent", name, opts=opts)
          # Create resources
          self.resource = aws.ec2.Instance(...)
          export("output", self.resource.id)
  ```

### Tags
- Use `ol_infrastructure.lib.ol_types.BusinessUnit` enum for OU tags
- Apply consistent tags to all resources
- Standard tags: `Name`, `Environment`, `ManagedBy`, `BusinessUnit`

### IAM Policies
- Lint with `ol_infrastructure.lib.aws.iam_helper.lint_iam_policy()`
- Follow least-privilege principle
- Document policy intent with comments
- Use policy variables for dynamic ARNs

### Code Organization
- Keep `__main__.py` focused on resource creation
- Move complex logic to separate modules
- Use components for multi-resource groups
- Use helpers for shared functionality

## Packer/PyInfra Code Style

### HCL Formatting
- Always run `packer fmt -recursive src/bilder/` before committing
- Use 2-space indentation (Packer standard)
- Use descriptive variable names

### PyInfra Scripts (`deploy.py`)
- Use type hints for function arguments
- Organize operations by category (packages, files, services)
- Include comments for non-obvious configuration
- Example:
  ```python
  from pyinfra import host
  from pyinfra.operations import apt, systemd

  # Install base packages
  apt.packages(packages=["curl", "jq"])

  # Configure and start service
  systemd.service("myapp", running=True, enabled=True)
  ```

### Component Reuse
- Store reusable provisioning logic in `src/bilder/components/`
- Create separate modules for each component type
- Document component parameters and effects

## Common Patterns

### Configuration Management
- Use Pydantic `BaseModel` for all configurations
- Validate inputs at component initialization
- Provide sensible defaults
- Document all configuration options

### Resource Naming
- Use lowercase with hyphens for resource names
- Include environment in name: `app-name-qa`, `app-name-prod`
- Use descriptive names that indicate purpose

### Error Handling
- Use type hints to indicate fallible operations
- Raise specific exceptions with descriptive messages
- Document error conditions in docstrings

## Linting Configuration

All linting is configured in `pyproject.toml`:

```toml
[tool.ruff]
line-length = 88
target-version = "py312"

[tool.mypy]
python_version = "3.12"
strict = true

[tool.ruff.lint]
select = ["E", "F", "W", "I"]  # errors, pyflakes, warnings, isort
```

Run validation:
```bash
uv run ruff format src/    # Auto-format
uv run ruff check src/     # Check for issues
uv run mypy src/           # Type check
```
