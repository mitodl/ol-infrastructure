from ol_concourse.lib.jobs.infrastructure import pulumi_jobs_chain
from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import Identifier
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS

eks_infrastructure_code = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/infrastructure/aws/eks",
        *PULUMI_WATCHED_PATHS,
        "src/bridge/lib/versions.py",
    ],
)

eks_substructure_code = git_repo(
    Identifier("ol-infrastructure-substructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/substructure/aws/eks",
        *PULUMI_WATCHED_PATHS,
        "src/bridge/lib/versions.py",
    ],
)

pipeline_fragments = []

for cluster in ["data", "operations", "applications", "residential"]:
    stages = ["CI", "QA", "Production"]

    infra_chain = pulumi_jobs_chain(
        eks_infrastructure_code,
        project_name="ol-infrastructure-eks",
        project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/aws/eks"),
        stack_names=[f"infrastructure.aws.eks.{cluster}.{stage}" for stage in stages],
    )
    pipeline_fragments.append(infra_chain)

    substructure_chain = pulumi_jobs_chain(
        eks_substructure_code,
        project_name="ol-substructure-eks",
        project_source_path=PULUMI_CODE_PATH.joinpath("substructure/aws/eks"),
        stack_names=[f"substructure.aws.eks.{cluster}.{stage}" for stage in stages],
    )
    pipeline_fragments.append(substructure_chain)

eks_cluster_update_pipeline = PipelineFragment.combine_fragments(
    *pipeline_fragments
).to_pipeline()

eks_cluster_update_pipeline.resources.append(eks_infrastructure_code)
eks_cluster_update_pipeline.resources.append(eks_substructure_code)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(eks_cluster_update_pipeline.model_dump_json(indent=2))
    sys.stdout.write(eks_cluster_update_pipeline.model_dump_json(indent=2))
    sys.stdout.writelines(
        [
            "\n",
            (
                "fly -t <target> set-pipeline -p pulumi-eks-cluster-update -c"
                " definition.json"
            ),
        ]
    )
