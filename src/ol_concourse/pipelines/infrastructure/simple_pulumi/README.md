# Simple Pulumi Meta Pipeline

This directory contains a meta pipeline that manages Concourse pipelines for applications following the **Simple Pulumi-Only** pattern.

## Pattern Description

The Simple Pulumi-Only pattern is for applications/services that:
- Only need Pulumi infrastructure deployment
- Deploy across standard stages: CI → QA → Production
- Have **no build steps** (no Docker builds, no Packer AMIs)
- Are triggered solely by infrastructure code changes

## Managed Applications

The following applications are currently managed by this meta pipeline:

1. **airbyte** - Airbyte data integration platform
2. **bootcamps** - Bootcamps application
3. **digital-credentials** - Digital credentials service
4. **fastly-redirector** - Fastly redirector service
5. **kubewatch** - Kubernetes cluster monitoring
6. **micromasters** - MicroMasters application (Pulumi-only)
7. **mongodb-atlas** - MongoDB Atlas infrastructure
8. **ocw-studio** - OCW Studio content management (Pulumi-only)
9. **open-discussions** - Open Discussions platform (Pulumi-only, QA/Production only)
10. **open-metadata** - OpenMetadata data catalog
11. **opensearch** - OpenSearch search and analytics cluster
12. **tika** - Apache Tika document processing service
13. **vector-log-proxy** - Vector log proxy service
14. **xpro** - xPRO application
15. **xpro-partner-dns** - xPRO partner DNS configuration

## Files

- **`meta.py`**: Meta pipeline generator that creates/updates individual app pipelines
- **`definition.json`**: Generated pipeline definition for the meta-pipeline
- **`simple_pulumi_pipeline.py`**: Template for generating individual app pipelines
- **`__init__.py`**: Package initialization

## Usage

### Deploying the Meta Pipeline

```bash
cd src/ol_concourse/pipelines/infrastructure/simple_pulumi/
python meta.py
fly -t pr-inf sp -p simple-pulumi-meta -c definition.json
```

### Adding a New Application

1. Add configuration to `pipeline_params` in `simple_pulumi_pipeline.py`:

```python
pipeline_params: dict[str, SimplePulumiParams] = {
    # ... existing apps ...
    "my-new-app": SimplePulumiParams(
        app_name="my-new-app",
        pulumi_project_path="applications/my_new_app/",
        stack_prefix="applications.my_new_app",
        pulumi_project_name="ol-infrastructure-my-new-app",  # optional
        stages=["CI", "QA", "Production"],  # optional, this is default
        branch="main",  # optional, this is default
    ),
}
```

2. Add the app name to the list in `meta.py`:

```python
app_names = [
    # ... existing apps ...
    "my-new-app",
]
```

3. Regenerate and deploy the meta pipeline (which will create the new app pipeline):

```bash
python meta.py
fly -t pr-inf sp -p simple-pulumi-meta -c definition.json
```

### Testing an Individual Pipeline Locally

You can test generating a single app pipeline:

```bash
python simple_pulumi_pipeline.py tika
```

This outputs the pipeline definition to both `definition.json` and stdout.

## Parameters

### SimplePulumiParams

```python
class SimplePulumiParams(BaseModel):
    app_name: str                           # Application name (required)
    pulumi_project_path: str                # Path relative to src/ol_infrastructure/ (required)
    stack_prefix: str                       # Pulumi stack prefix (required)
    pulumi_project_name: str                # Defaults to "ol-infrastructure-{app_name}"
    stages: list[str]                       # Defaults to ["CI", "QA", "Production"]
    deployment_groups: list[str] | None     # Deployment groups for multi-group projects
    auto_discover_stacks: bool              # Auto-discover stacks from filesystem
    additional_watched_paths: list[str]     # Extra paths to watch (default: [])
    branch: str                             # Git branch (default: "main")
```

**Note**: Setting `deployment_groups` automatically enables `auto_discover_stacks`.

## How It Works

### Meta Pipeline Flow

1. **Git Resource**: Watches for changes to the meta pipeline code
2. **Per-App Jobs**: For each app in `app_names`:
   - Fetches pipeline definitions from git
   - Runs `simple_pulumi_pipeline.py {app_name}`
   - Sets the generated pipeline as `pulumi-{app_name}`
3. **Self-Update Job**: Updates the meta pipeline itself when code changes

### Individual Pipeline Flow

Each generated app pipeline:
1. **Git Resource**: Watches Pulumi code for the specific app
2. **Pulumi Jobs Chain**: Deploys infrastructure to CI → QA → Production
   - Each stage can be deployed independently
   - Production requires manual trigger

## Benefits

- **Consistency**: All apps use identical pipeline structure
- **Maintainability**: Update once, applies to all apps
- **Easy Onboarding**: Add new app by updating two lists
- **Self-Updating**: Meta pipeline updates itself and all app pipelines
- **Reduced Duplication**: Single template instead of multiple individual pipeline files

## Migration from Old Pipelines

If you're migrating an existing pipeline to this pattern:

1. Verify the app follows the simple Pulumi-only pattern (no builds)
2. Extract parameters from the old pipeline file
3. Add configuration to `simple_pulumi_pipeline.py`
4. Add app name to `meta.py`
5. Deploy meta pipeline
6. Verify new pipeline works correctly
7. (Optional) Archive or delete old pipeline file

## Troubleshooting

### Pipeline not generating

- Check app name is in both `pipeline_params` (simple_pulumi_pipeline.py) and `app_names` (meta.py)
- Verify the pipeline definitions repository is accessible
- Check Concourse logs for the specific job

### Stack name mismatch

- Ensure `stack_prefix` matches your actual Pulumi stack names
- Stack names are generated as: `{stack_prefix}.{stage}`
- Example: `applications.tika.CI`, `applications.tika.QA`, `applications.tika.Production`

### Path not triggering pipeline

- Add the path to `additional_watched_paths` parameter
- Paths are relative to repository root
- Default watched paths include `PULUMI_WATCHED_PATHS` and the app's Pulumi directory

## Examples

### Simple Application Configuration

```python
"tika": SimplePulumiParams(
    app_name="tika",
    pulumi_project_path="applications/tika/",
    stack_prefix="applications.tika",
),
```

### Infrastructure (Not Application) Configuration

```python
"mongodb-atlas": SimplePulumiParams(
    app_name="mongodb-atlas",
    pulumi_project_path="infrastructure/mongodb_atlas/",
    stack_prefix="infrastructure.mongodb_atlas",
),
```

### Additional Watched Paths

Some apps need to watch additional directories beyond standard Pulumi paths:

```python
"ocw-studio": SimplePulumiParams(
    app_name="ocw-studio",
    pulumi_project_path="applications/ocw_studio/",
    stack_prefix="applications.ocw_studio",
    additional_watched_paths=["src/bridge/secrets/ocw_studio/"],
),
```

### Custom Stages Configuration

Some applications don't deploy to all standard environments:

```python
"open-discussions": SimplePulumiParams(
    app_name="open-discussions",
    pulumi_project_path="applications/open_discussions/",
    stack_prefix="applications.open_discussions",
    stages=["QA", "Production"],  # No CI environment
),
```

### Auto-Discovery of Stacks

For projects with multiple deployment groups (like mongodb_atlas with mitx, mitxonline, xpro, etc.),
you can use auto-discovery to automatically find all stacks:

```python
"mongodb-atlas": SimplePulumiParams(
    app_name="mongodb-atlas",
    pulumi_project_path="infrastructure/mongodb_atlas/",
    stack_prefix="infrastructure.mongodb_atlas",
    deployment_groups=["mitx", "mitx-staging", "mitxonline", "xpro"],
    auto_discover_stacks=True,  # Automatically enabled when deployment_groups is set
),
```

This will:
1. Scan the Pulumi project directory for stack files
2. Match stacks with the pattern: `{stack_prefix}.{group}.{stage}`
3. Filter to only the specified deployment groups
4. For each group, sequence stages as CI → QA → Production (if they exist)
5. Create **independent parallel job chains** for each deployment group

**Parallel Execution**: Each deployment group runs independently:
```
mitx:          CI → QA → Production
mitx-staging:  CI → QA → Production  } All groups run in parallel
mitxonline:    CI → QA → Production
xpro:          CI → QA → Production
```

Example: mongodb_atlas generates 12 jobs (4 groups × 3 stages) organized as 4 parallel chains.
All CI jobs can run simultaneously, then all QA jobs, then all Production jobs.

```python
"custom-app": SimplePulumiParams(
    app_name="custom-app",
    pulumi_project_path="applications/custom_app/",
    stack_prefix="applications.custom_app",
    stages=["Development", "Staging", "Production"],
    branch="develop",
    additional_watched_paths=["src/bridge/lib/custom_helpers.py"],
),
```
