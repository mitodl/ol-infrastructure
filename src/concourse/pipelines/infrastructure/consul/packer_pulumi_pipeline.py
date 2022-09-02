import itertools

from concourse.lib.constants import PULUMI_CODE_PATH
from concourse.lib.jobs.infrastructure import packer_jobs, pulumi_jobs_chain
from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import GetStep, Identifier, Pipeline
from concourse.lib.resource_types import hashicorp_resource
from concourse.lib.resources import git_repo, hashicorp_release

consul_release = hashicorp_release(Identifier("consul-release"), "consul")
consul_image_code = git_repo(
    Identifier("ol-infrastructure-packer"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/bilder/components/",
        "src/bilder/images/consul/",
        "src/bilder/components/hashicorp/",
        "src/bilder/images/packer.pkr.hcl",
        "src/bilder/images/variables.pkr.hcl",
        "src/bilder/images/config.pkr.hcl",
    ],
)
consul_pulumi_code = git_repo(
    name=Identifier("ol-infrastructure-pulumi"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=[
        "src/ol_infrastructure/infrastructure/consul/",
        "src/ol_infrastructure/substructure/consul/",
        "pipelines/infrastructure/scripts/",
    ],
)

get_consul_release = GetStep(get=consul_release.name, trigger=True)

consul_ami_fragment = packer_jobs(
    dependencies=[get_consul_release],
    image_code=consul_image_code,
    packer_template_path="src/bilder/images/",
    packer_vars={"app_name": "consul"},
    node_types=["server"],
    env_vars_from_files={"CONSUL_VERSION": "consul-release/version"},
    extra_packer_params={"only": ["amazon-ebs.third-party"]},
)

consul_pulumi_fragments = []
for network in [
    "mitx",
    "mitx-staging",
    "mitxonline",
    "apps",
    "data",
    "operations",
    "xpro",
]:
    # Missing a few stacks for some apps
    if network in ["apps", "data"]:
        stages = ("QA", "Production")
    else:
        stages = ("CI", "QA", "Production")  # type: ignore
    consul_pulumi_fragment = pulumi_jobs_chain(
        consul_pulumi_code,
        project_name="ol-infrastructure-consul-server",
        stack_names=[f"infrastructure.consul.{network}.{stage}" for stage in stages],
        project_source_path=PULUMI_CODE_PATH.joinpath("infrastructure/consul/"),
        dependencies=[
            GetStep(
                get=consul_ami_fragment.resources[-1].name,
                trigger=True,
                passed=[consul_ami_fragment.jobs[-1].name],
            )
        ],
    )
    consul_pulumi_fragments.append(consul_pulumi_fragment)


pulumi_resource_types = list(
    itertools.chain.from_iterable(
        [pulumi_fragment.resource_types for pulumi_fragment in consul_pulumi_fragments]
    )
)
pulumi_resources = list(
    itertools.chain.from_iterable(
        [pulumi_fragment.resources for pulumi_fragment in consul_pulumi_fragments]
    )
)
pulumi_jobs = list(
    itertools.chain.from_iterable(
        [pulumi_fragment.jobs for pulumi_fragment in consul_pulumi_fragments]
    )
)

combined_fragment = PipelineFragment(
    resource_types=consul_ami_fragment.resource_types
    + pulumi_resource_types
    + [hashicorp_resource()],
    resources=consul_ami_fragment.resources + pulumi_resources,
    jobs=consul_ami_fragment.jobs + pulumi_jobs,
)

consul_pipeline = Pipeline(
    resource_types=combined_fragment.resource_types,
    resources=combined_fragment.resources
    + [consul_image_code, consul_release, consul_pulumi_code],
    jobs=combined_fragment.jobs,
)


if __name__ == "__main__":
    import sys

    with open("definition.json", "wt") as definition:
        definition.write(consul_pipeline.json(indent=2))
    sys.stdout.write(consul_pipeline.json(indent=2))
