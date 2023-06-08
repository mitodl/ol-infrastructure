from concourse.pipelines.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import pulumi_job
from concourse.lib.models.pipeline import Identifier, Pipeline
from concourse.lib.resources import git_repo


xpro_dns_code = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/substructure/xpro_partner_dns",
    ],
)

pulumi_job_fragment = pulumi_job(
    xpro_dns_code,
    stack_name="substructure.xpro_partner_dns",
    project_name="ol-infrastructure-substructure-xpro-partner-dns",
    project_source_path=PULUMI_CODE_PATH.joinpath("substructure/xpro_partner_dns/"),
)

xpro_partner_dns_pipeline = Pipeline(
    resource_types=pulumi_job_fragment.resource_types,
    resources=[xpro_dns_code, *pulumi_job_fragment.resources],
    jobs=pulumi_job_fragment.jobs,
)

if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:
        definition.write(xpro_partner_dns_pipeline.json(indent=2))
    sys.stdout.write(xpro_partner_dns_pipeline.json(indent=2))
    sys.stdout.writelines(
        [
            "\n",
            "fly -t <target> set-pipeline -p pulumi-xpro-partner-dns -c definition.json",  # noqa: E501
        ]
    )
