import sys

from ol_concourse.lib.models.fragment import PipelineFragment
from ol_concourse.lib.models.pipeline import GetStep, GroupConfig, Job, Pipeline
from ol_concourse.lib.resources import Identifier, schedule
from ol_concourse.lib.tasks import (
    block_for_instance_refresh_task,
    instance_refresh_task,
)

application = "edxapp"
product = "xpro"
# environments = ["ci", "qa", "production"] # noqa: ERA001
environments = ["ci"]
node_classes = ["worker-xpro", "web-xpro"]

build_schedule = schedule(Identifier("build-schedule"), interval="480h")

jobs = []
group_configs = []
for env in environments:
    job_names = []
    for node_class in node_classes:
        filter_template = f"Name=tag:Name,Values={application}-{node_class} Name=tag:Environment,Values={product}-{env}"  # noqa: E501
        query = "AutoScalingGroups[*].AutoScalingGroupName"
        refresh_job = Job(
            name=f"{env}-{node_class}-instance-refresh",
            plan=[
                GetStep(get="build-schedule", trigger=True),
                instance_refresh_task(
                    filters=filter_template, node_class=node_class, queries=query
                ),
                block_for_instance_refresh_task(
                    filters=filter_template,
                    queries=query,
                    node_class=node_class,
                    check_freq=10,
                ),
            ],
        )
        jobs.append(refresh_job)
        job_names.append(refresh_job.name)
    env_group_config = GroupConfig(
        name=env,
        jobs=job_names,
    )
    group_configs.append(env_group_config)


instance_refresh_pipeline_fragment = PipelineFragment(
    resource_types=[],
    resources=[build_schedule],
    jobs=jobs,
)

instance_refresh_pipeline = Pipeline(
    resource_types=instance_refresh_pipeline_fragment.resource_types,
    resources=instance_refresh_pipeline_fragment.resources,
    jobs=instance_refresh_pipeline_fragment.jobs,
    groups=group_configs,
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(instance_refresh_pipeline.model_dump_json(indent=2))
    sys.stdout.write(instance_refresh_pipeline.model_dump_json(indent=2))
    print()  # noqa: T201
    print(  # noqa: T201
        "fly -t infra-prod sp -p instance-refresh-xpro -c definition.json"
    )  # noqa: RUF100, T201
