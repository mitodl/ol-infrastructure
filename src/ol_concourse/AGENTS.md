# Agent Instructions — `src/ol_concourse`

Pydantic-model-based DSL for generating Concourse CI/CD pipeline YAML. Pipelines are
Python scripts that build a `Pipeline` object and serialize it to JSON; they are never
raw YAML.

## How Pipelines Work

```
pipelines/<category>/<name>/pipeline.py   # builds Pipeline object, writes definition.json
    → python pipeline.py               # writes definition.json; prints fly command to stdout
    → fly -t <target> sp -p <pipeline-name> -c definition.json
```

Each `pipeline.py` is a standalone script. Running it **writes `definition.json` directly**
(via `open("definition.json", "w")`) and prints the `fly set-pipeline` command to stdout.
Do not redirect stdout to a file — that would mix the JSON with the fly command.

## Directory Layout

```
pipelines/
  constants.py            # Pipeline-level constants (ECR_REGION, PULUMI_WATCHED_PATHS, etc.)
  jobs.py                 # Top-level job factories (packer_jobs, pulumi_jobs_chain, pulumi_job)
  infrastructure/         # Platform infra pipelines (consul, vault, eks, dagster, etc.)
  applications/           # Application deployment pipelines
```

The `ol_concourse.lib` modules (models, resources, resource_types, containers) are provided
by the `ol-concourse` pip package (installed into the venv). Browse them under
`.venv/lib/pythonX.Y/site-packages/ol_concourse/lib/` or via the upstream repo. Do not
create a local `lib/` directory here.

## Writing a New Pipeline

1. Create `pipelines/<category>/<name>/pipeline.py` with a `__init__.py`.
2. Build resources, then jobs, then return a `Pipeline`.
3. Use factory functions from `lib/` — don't construct raw `Job`/`Resource` dicts.

```python
import sys
from ol_concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_WATCHED_PATHS
from ol_concourse.pipelines.jobs import pulumi_jobs_chain

code = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=["src/ol_infrastructure/applications/myapp/", *PULUMI_WATCHED_PATHS],
)

pipeline = Pipeline(
    resources=[code],
    jobs=pulumi_jobs_chain(
        code_resource=code,
        environments=["CI", "QA", "Production"],
        project_path="src/ol_infrastructure/applications/myapp/",
    ),
)

if __name__ == "__main__":
    output = pipeline.model_dump_json(indent=2)
    with open("definition.json", "w") as f:
        f.write(output)
    sys.stdout.write(output)
    print()
    print("fly -t pr-inf sp -p myapp -c definition.json")
```

## Key Factories

| Factory | Location | What it builds |
|---------|----------|---------------|
| `git_repo(name, uri, paths)` | `lib/resources.py` | Git resource watching specific paths |
| `registry_image(name, ...)` | `lib/resources.py` | Docker registry image resource |
| `pulumi_provisioner(...)` | `lib/resources.py` | Pulumi provisioner resource |
| `hashicorp_release(name, product)` | `lib/resources.py` | HashiCorp release resource |
| `packer_jobs(...)` | `pipelines/jobs.py` | AMI build job chain |
| `pulumi_jobs_chain(...)` | `pipelines/jobs.py` | Multi-env Pulumi deploy chain |
| `pulumi_job(...)` | `pipelines/jobs.py` | Single-env Pulumi deploy job |
| `container_build_task(...)` | `lib/containers.py` | Docker image build task config |

## Pydantic Model Rules

- `Identifier` is a validated string type — all resource/job names must be `Identifier(...)`, not bare strings
- `PipelineFragment` composes multiple jobs and resources into a reusable unit
- All step types (`GetStep`, `PutStep`, `TaskStep`, `SetPipelineStep`, etc.) are in `lib/models/pipeline.py`
- Use `model_dump_json(indent=2)` to serialize — never `json.dumps` a pipeline manually

## Adding a Pipeline to the Meta Pipeline

Infrastructure pipelines are self-managed via `pipelines/infrastructure/meta.py`. After
creating a new pipeline, add an entry to `PIPELINE_CONFIGS` in that file:

```python
PIPELINE_CONFIGS: list[tuple[str, str]] = [
    ("my-pipeline-name", "src/ol_concourse/pipelines/infrastructure/myapp/pipeline.py"),
    ...
]
```

Then regenerate and re-apply the meta pipeline itself.

## Validation

```bash
uv run ruff format src/ol_concourse/
uv run ruff check src/ol_concourse/
uv run mypy src/ol_concourse/

# Render a pipeline and validate the output JSON is well-formed
cd src/ol_concourse/pipelines/infrastructure/dagster/
python pipeline.py && python -m json.tool definition.json > /dev/null && echo "OK"
```

## Common Mistakes

- **Bare strings as names** — always wrap in `Identifier("...")`, validation will fail at runtime otherwise
- **Importing from `pipeline.py` in other files** — each pipeline is a standalone script, not a library
- **Writing raw YAML** — if you're writing YAML, you're doing it wrong; use the model DSL
- **Forgetting `trigger=True`** on `GetStep` — pipelines won't auto-trigger without it
