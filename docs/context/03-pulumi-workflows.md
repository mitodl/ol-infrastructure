# Pulumi Workflows

## Project Structure

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

## Running Pulumi Commands

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

## Key Pulumi Helpers

- `ol_infrastructure.lib.pulumi_helper.parse_stack()` — Returns `StackInfo` dataclass with stack name/environment
- `ol_infrastructure.lib.ol_types` — Pydantic models for configurations (inherit from `AWSBase`)

## Code Style & Conventions

### Pulumi
- **Projects:** Each `Pulumi.yaml` directory is a project
- **Stacks:** Named as `<namespace>.<environment>` (e.g., `infrastructure.aws.network.QA`)
- **Tags:** Use `ol_infrastructure.lib.ol_types.BusinessUnit` enum for OU tags
- **IAM policies:** Lint with `ol_infrastructure.lib.aws.iam_helper.lint_iam_policy()`

## File Modification Tasks

| Task | Files to Modify | Validation |
|------|----------------|------------|
| Add AWS resource | `src/ol_infrastructure/infrastructure/aws/<service>/__main__.py` | `pulumi preview` |
| Create new app infra | Create `src/ol_infrastructure/applications/<app>/` with `Pulumi.yaml` + `__main__.py` | `pulumi stack init` then `preview` |
| Add reusable component | `src/ol_infrastructure/components/<category>/` | Used in other projects + unit tests |
| Modify policy or IAM | Update relevant `__main__.py`, lint with `iam_helper` | `pulumi preview` |

## Component Reuse

Create reusable components in `src/ol_infrastructure/components/` to avoid duplication across projects. Components should:

1. Accept configurable parameters via Pydantic models
2. Return component outputs that other resources can depend on
3. Include comprehensive type hints
4. Have unit tests with Pulumi mocks

Example component structure:
```python
from pulumi import ComponentResource, export
from ol_infrastructure.lib.ol_types import AWSBase

class MyComponent(ComponentResource):
    def __init__(self, name: str, config: MyConfig, opts=None):
        super().__init__("custom:resource:MyComponent", name, opts=opts)
        # Create resources and export outputs
        self.output = export("value")
```
