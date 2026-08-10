"""Deploy the Azure OpenAI infrastructure across CI, QA, and Production.

Kept separate from the Vault pipeline deliberately: this project provisions Azure
resources and is not a Vault substructure. The Vault Azure secrets engine
(``substructure/vault/azure``, deployed by ``packer-pulumi-vault``) consumes this
project's stack outputs, so this pipeline must have run for an environment before
that one can succeed there.
"""

from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo

from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS
from ol_concourse.pipelines.jobs import pulumi_jobs_chain
from ol_concourse.pipelines.secrets_map import project_secrets_paths

azure_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-azure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        *PULUMI_WATCHED_PATHS,
        "src/ol_infrastructure/infrastructure/azure/",
        *project_secrets_paths("infrastructure/azure/openai/"),
    ],
)

azure_openai_fragment = pulumi_jobs_chain(
    azure_pulumi_code,
    refresh_stack=True,
    project_name="ol-infrastructure-azure-openai",
    stack_names=["CI", "QA", "Production"],
    project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/azure/openai/"),
)

combined_fragment = PipelineFragment.combine_fragments(azure_openai_fragment)

azure_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[*combined_fragment.resources, azure_pulumi_code],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(azure_pipeline.model_dump_json(indent=2))
    sys.stdout.write(azure_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p pulumi-azure -c definition.json")  # noqa: T201
