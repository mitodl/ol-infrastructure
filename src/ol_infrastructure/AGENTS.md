# Agent Instructions — `src/ol_infrastructure`

Pulumi infrastructure-as-code for MIT Open Learning. Each subdirectory has a distinct role;
place code in the right one before writing anything.

## Directory Roles

| Directory | Purpose | When to add code here |
|-----------|---------|----------------------|
| `applications/<app>/` | Per-app Pulumi stacks (Pulumi.yaml entrypoints) | New or existing application infrastructure |
| `components/aws/` | Reusable AWS Pulumi ComponentResources | Any AWS resource group used in 2+ places |
| `components/services/` | Reusable third-party service components (k8s operators, Vault, etc.) | Same as above but for non-AWS services |
| `components/applications/` | App-level reusable components | App patterns shared across stacks |
| `infrastructure/aws/<service>/` | Shared AWS platform infrastructure (VPCs, EKS clusters, IAM, KMS, DNS) | Platform-level resources not tied to a single app |
| `lib/` | Pure-Python helpers (no Pulumi resources) | Shared logic, type definitions, Pulumi helpers |
| `providers/` | Custom Pulumi provider wrappers | Wrapping external provider APIs |
| `saas/` | SaaS integrations (not AWS-native) | Rootly, etc. |
| `substructure/` | Cross-cutting Pulumi stacks (Consul, Vault) | Infrastructure that spans apps |

## Stack Structure

Every Pulumi project is a directory containing:

```
__main__.py              # Resource declarations
Pulumi.yaml              # Project definition
Pulumi.<env>.yaml        # Stack config per environment (QA, Production, CI, Dev)
```

After the project-scoped stack migration, stack names are short: `QA`, `Production`, `CI`,
or `<tenant>.QA` for multi-tenant projects. Select from inside the project directory.

```bash
cd src/ol_infrastructure/applications/mit_learn/
pulumi stack ls
pulumi stack select QA
pulumi preview    # always preview before up
```

## Components: Non-Negotiable Rules

Components in `components/` **must**:

1. Inherit from `pulumi.ComponentResource`
2. Accept config via a Pydantic model (inherit from `ol_infrastructure.lib.ol_types.AWSBase` for AWS config)
3. Export outputs as instance attributes
4. Have unit tests using Pulumi mocks (in `tests/ol_infrastructure/components/`)

```python
import pulumi
from ol_infrastructure.lib.ol_types import AWSBase

class MyConfig(AWSBase):
    instance_count: int = 1

class MyComponent(pulumi.ComponentResource):
    def __init__(self, name: str, config: MyConfig, opts: pulumi.ResourceOptions | None = None):
        super().__init__("ol:aws:MyComponent", name, opts=opts)
        # create child resources with opts=pulumi.ResourceOptions(parent=self)
```

## Key Helpers

- `ol_infrastructure.lib.pulumi_helper.parse_stack()` → `StackInfo` (stack name, environment)
- `ol_infrastructure.lib.ol_types.AWSBase` → Pydantic base for all AWS configs
- `ol_infrastructure.lib.ol_types.BusinessUnit` → enum for OU tags; apply to all resources
- `ol_infrastructure.lib.aws.iam_helper.lint_iam_policy()` → validate IAM policy docs before use
- `bridge.lib.magic_numbers` → canonical port/size constants; don't hardcode these values

## Common Patterns

### Reading stack config

```python
from ol_infrastructure.lib.pulumi_helper import parse_stack
stack_info = parse_stack()
env_upper = stack_info.name        # "QA" | "Production" | "CI" | "Dev"
env_lower = stack_info.env_suffix  # "qa" | "production" | "ci" | "dev"
```

### IAM policy

```python
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
policy_doc = {"Version": "2012-10-17", "Statement": [...]}
lint_iam_policy(policy_doc)  # raises on errors; signature: (policy_document, stringify=False)
```

### Resource tags

`AWSBase` requires `OU` and `Environment` tags. `OU` must be a valid `BusinessUnit` value.

```python
from ol_infrastructure.lib.ol_types import BusinessUnit
tags = {"OU": BusinessUnit.operations, "Environment": stack_info.name}
```

## Validation

```bash
uv run ruff format src/ol_infrastructure/
uv run ruff check src/ol_infrastructure/
uv run mypy src/ol_infrastructure/
pulumi preview    # from inside the project directory
uv run pytest tests/ol_infrastructure/
```

## Where NOT to put code

- Logic used in only one stack → keep it in that stack's `__main__.py`
- PyInfra/Packer provisioning → belongs in `src/bilder/`, not here
- Concourse pipeline definitions → belongs in `src/ol_concourse/`
- Glue between bilder and ol_infrastructure → belongs in `src/bridge/`
