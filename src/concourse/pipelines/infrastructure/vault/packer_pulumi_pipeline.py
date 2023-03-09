from concourse.lib.constants import PULUMI_CODE_PATH, PULUMI_WATCHED_PATHS
from concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Pipeline,
    Resource,
    ResourceType,
    Job,
)
from concourse.lib.resource_types import hashicorp_resource
from concourse.lib.resources import git_repo, hashicorp_release


vault_release = hashicorp_release(Identifier("vault-release"), "vault")
vault_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/vault/",
        "src/bilder/components/hashicorp/",
        "src/bilder/images/packer.pkr.hcl",
        "src/bilder/images/variables.pkr.hcl",
        "src/bilder/images/config.pkr.hcl",
    ],
)
vault_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[*PULUMI_WATCHED_PATHS, "src/ol_infrastructure/infrastructure/vault/"],
)
vault_pulumi_substructure_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi-substructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[*PULUMI_WATCHED_PATHS, "src/ol_infrastructure/substructure/vault/"],
)

get_vault_release = GetStep(get=vault_release.name, trigger=True)

vault_ami_fragment = packer_jobs(
    dependencies=[get_vault_release],
    image_code=vault_image_code,
    packer_template_path="src/bilder/images/",
    packer_vars={"app_name": "vault"},
    node_types=["server"],
    env_vars_from_files={"VAULT_VERSION": "vault-release/version"},
    extra_packer_params={"only": ["amazon-ebs.third-party"]},
)

vault_pulumi_fragment = pulumi_jobs_chain(
    vault_pulumi_code,
    project_name="ol-infrastructure-vault-server",
    stack_names=[
        f"infrastructure.vault.operations.{stage}"
        for stage in ("CI", "QA", "Production")
    ],
    project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/vault/"),
    dependencies=[
        GetStep(
            get=vault_ami_fragment.resources[-1].name,
            trigger=True,
            passed=[vault_ami_fragment.jobs[-1].name],
        )
    ],
)

substructure_resource_types: list[ResourceType] = []
substructure_resources: list[Resource] = []
substructure_jobs: list[Job] = []

for substructure in ["pki"]:
    substructure_fragment = pulumi_jobs_chain(
        vault_pulumi_substructure_code,
        project_name="ol-infrastructure-vault-substructure",
        stack_names=[
            f"substructure.vault.{substructure}.operations.{stage}"
            for stage in ("CI", "QA", "Production")
        ],
        project_source_path=PULUMI_CODE_PATH.joinpath(
            "substructure/vault/{substucture}/"
        ),
    )
    substructure_resource_types = (
        substructure_resource_types + substructure_fragment.resource_types
    )
    substructure_resources = substructure_resources + substructure_fragment.resources
    substructure_jobs = substructure_jobs = substructure_fragment.jobs

combined_fragment = PipelineFragment(
    resource_types=vault_ami_fragment.resource_types
    + vault_pulumi_fragment.resource_types
    + substructure_resource_types
    + [hashicorp_resource()],
    resources=vault_ami_fragment.resources
    + vault_pulumi_fragment.resources
    + substructure_resources,
    jobs=vault_ami_fragment.jobs + vault_pulumi_fragment.jobs + substructure_jobs,
)

vault_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=[
        *combined_fragment.resources,
        vault_image_code,
        vault_release,
        vault_pulumi_code,
        vault_pulumi_substructure_code,
    ],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:
        definition.write(vault_pipeline.json(indent=2))
    sys.stdout.write(vault_pipeline.json(indent=2))
    print()
    print("fly -t pr-inf sp -p packer-pulumi-vault -c definition.json")
