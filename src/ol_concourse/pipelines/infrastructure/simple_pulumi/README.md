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

1. **fastly-redirector** - Fastly redirector service
2. **tika** - Apache Tika document processing service
3. **airbyte** - Airbyte data integration platform
4. **kubewatch** - Kubernetes cluster monitoring
5. **digital-credentials** - Digital credentials service
6. **open-metadata** - OpenMetadata data catalog
7. **xpro-partner-dns** - xPRO partner DNS configuration
8. **mongodb-atlas** - MongoDB Atlas infrastructure
9. **vector-log-proxy** - Vector log proxy service

## Files

- **`meta.py`**: Meta pipeline generator that creates/updates individual app pipelines
- **`simple_pulumi_pipeline.py`**: Template for generating individual app pipelines
- **`__init__.py`**: Package initialization

## Usage

### Deploying the Meta Pipeline

```bash
cd src/ol_concourse/pipelines/infrastructure/simple_pulumi/
python meta.py > definition.json
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
python meta.py > definition.json
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
    pulumi_project_name: str | None         # Defaults to "ol-infrastructure-{app_name}"
    stages: list[str]                       # Defaults to ["CI", "QA", "Production"]
    additional_watched_paths: list[str]     # Extra paths to watch (default: [])
    branch: str                             # Git branch (default: "main")
```

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
- **Reduced Duplication**: Single template instead of 9+ individual pipeline files

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

### Custom Configuration

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
