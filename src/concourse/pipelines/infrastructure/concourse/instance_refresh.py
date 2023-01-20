import sys

from concourse.lib.models.fragment import PipelineFragment
from concourse.lib.models.pipeline import GroupConfig, Job, Pipeline
from concourse.lib.tasks import block_for_instance_refresh_task, instance_refresh_task

environments = ["ci", "qa", "production"]
node_classes = ["worker-infra", "worker-ocw", "worker-generic", "web"]

jobs = []
group_configs = []
for env in environments:
    job_names = []
    for node_class in node_classes:
        filter_template = f"Name=tag:concourse_type,Values={node_class} Name=tag:Environment,Values=operations-{env}"
        query = "AutoScalingGroups[*].AutoScalingGroupName"
        refresh_job = Job(
            name=f"{env}-{node_class}-instance-refresh",
            plan=[
                instance_refresh_task(filters=filter_template, queries=query),
                block_for_instance_refresh_task(
                    filters=filter_template, queries=query, check_freq=10
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
    resources=[],
    jobs=jobs,
)

instance_refresh_pipeline = Pipeline(
    resource_types=instance_refresh_pipeline_fragment.resource_types,
    resources=instance_refresh_pipeline_fragment.resources,
    jobs=instance_refresh_pipeline_fragment.jobs,
    groups=group_configs,
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:
        definition.write(instance_refresh_pipeline.json(indent=2))
    sys.stdout.write(instance_refresh_pipeline.json(indent=2))
    print()  # noqa: WPS421
    print(  # noqa: WPS421
        "fly -t pr-inf sp -p instance-refresh-concourse -c definition.json"  # noqa: C813
    )
