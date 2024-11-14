from ol_concourse.lib.jobs.infrastructure import pulumi_job
from ol_concourse.lib.models.pipeline import Identifier, Pipeline
from ol_concourse.lib.resources import git_repo
from ol_concourse.pipelines.constants import PULUMI_CODE_PATH

eks_cluster_code = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/infrastructure/aws/eks",
    ],
)

pulumi_job_fragment = pulumi_job(
    eks_cluster_code,
    stack_name="infrastructure.aws.eks",
    project_name="ol-infrastructure-infrastructure-aws-eks",
    project_source_path=PULUMI_CODE_PATH.joinpath("substructure/aws/eks/"),
)

eks_cluster_update_pipeline = Pipeline(
    resource_types=pulumi_job_fragment.resource_types,
    resources=[eks_cluster_code, *pulumi_job_fragment.resources],
    jobs=pulumi_job_fragment.jobs,
)

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
