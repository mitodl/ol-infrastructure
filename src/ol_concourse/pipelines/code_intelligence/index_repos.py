"""Concourse pipeline for indexing repositories into the shared code-graph-rag Memgraph.

This pipeline runs `cgr start --update-graph` for each configured repository whenever
changes are pushed to the main branch. The CLI performs incremental Project-aware
indexing using a hash cache, so only changed files are re-indexed.

⚠️  Do NOT use the `index_repository` MCP tool for CI indexing — it wipes all data for
    the project. This pipeline uses the `cgr start --update-graph` CLI exclusively.

Vault secret paths (KV v2, mount: secret-operations):
  code-graph-rag/concourse → memgraph_host, memgraph_port

Usage:
    python index_repos.py
    fly -t <target> set-pipeline -p code-graph-rag-index -c definition.json
"""

import sys

from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    Platform,
    Resource,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo

# Repositories to index. Each entry becomes a separate Concourse job so failures
# are isolated and retried independently.
# project_name must be unique across the shared Memgraph instance — it is the
# key used by code-graph-rag to scope all nodes and relationships.
REPOS_TO_INDEX: list[dict[str, str]] = [
    {
        "project_name": "ol-infrastructure",
        "uri": "https://github.com/mitodl/ol-infrastructure",
        "branch": "main",
    },
    {
        "project_name": "mit-learn",
        "uri": "https://github.com/mitodl/mit-learn",
        "branch": "main",
    },
    {
        "project_name": "ol-data-platform",
        "uri": "https://github.com/mitodl/ol-data-platform",
        "branch": "main",
    },
]

_index_script = """\
set -euo pipefail

echo "Indexing repository: ${PROJECT_NAME}"
echo "Memgraph: ${MEMGRAPH_HOST}:${MEMGRAPH_PORT}"

# cgr start --update-graph is Project-aware and incremental.
# It uses a .cgr_hash_cache to skip unchanged files and updates
# only the nodes/relationships that have changed since last run.
uvx --from 'code-graph-rag[treesitter-full]' cgr start \\
    --update-graph \\
    --repo-path "${REPO_PATH}" \\
    --project-name "${PROJECT_NAME}"

echo "Indexing complete for ${PROJECT_NAME}"
"""

# Build per-repo resources and jobs
git_resources: list[Resource] = []
jobs: list[Job] = []

for repo in REPOS_TO_INDEX:
    project_name = repo["project_name"]
    safe_name = project_name.replace("-", "_")
    resource_id = Identifier(f"{project_name}-repo")

    repo_resource = git_repo(
        name=resource_id,
        uri=repo["uri"],
        branch=repo["branch"],
        check_every="5m",
    )
    git_resources.append(repo_resource)

    jobs.append(
        Job(
            name=Identifier(f"index-{project_name}"),
            plan=[
                GetStep(get=resource_id, trigger=True),
                TaskStep(
                    task=Identifier(f"index-{project_name}-graph"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": "ghcr.io/astral-sh/uv",
                                "tag": "latest",
                            },
                        ),
                        inputs=[Input(name=resource_id)],
                        params={
                            "PROJECT_NAME": project_name,
                            "REPO_PATH": f"$(pwd)/{resource_id}",
                            "MEMGRAPH_HOST": (
                                "((code-graph-rag/concourse.memgraph_host))"
                            ),
                            "MEMGRAPH_PORT": (
                                "((code-graph-rag/concourse.memgraph_port))"
                            ),
                            "MEMGRAPH_USERNAME": "",
                            "MEMGRAPH_PASSWORD": "",
                        },
                        run=Command(
                            path="bash",
                            args=["-c", _index_script],
                        ),
                    ),
                ),
            ],
        )
    )

index_pipeline = Pipeline(
    resources=git_resources,
    jobs=jobs,
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(index_pipeline.model_dump_json(indent=2))
    sys.stdout.write(index_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p code-graph-rag-index -c definition.json\n"
    )
