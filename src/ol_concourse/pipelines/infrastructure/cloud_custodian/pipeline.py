from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    Platform,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, schedule

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

cloud_custodian_release = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
)

build_schedule = schedule(Identifier("build-schedule"), "24h")

custodian_registry_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source={
        "repository": dockerhub_ecr_image_uri("cloudcustodian/c7n"),
        "tag": "0.9.15.0",
        "aws_region": ECR_REGION,
    },
)

job_filename_dict = {
    "sync-ec2-tags": "sync_ec2_tags.yaml",
    "tag-ebs-resources-for-cleanup": "tag_ebs_resources_for_cleanup.yaml",
    "perform-ebs-cleanup": "cleanup_ebs_resources.yaml",
    "tag-packer-sg-for-cleanup": "tag_packer_security_groups_for_cleanup.yaml",
    "perform-packer-sg-cleanup": "cleanup_packer_security_groups.yaml",
}


def custodian_pipeline() -> Pipeline:
    job_list = []
    for name, filename in job_filename_dict.items():
        job_list.append(
            Job(
                name=name,
                plan=[
                    GetStep(get=cloud_custodian_release.name, trigger=False),
                    GetStep(get=build_schedule.name, trigger=True),
                    TaskStep(
                        task=Identifier(name),
                        config=TaskConfig(
                            platform=Platform.linux,
                            image_resource=custodian_registry_image,
                            inputs=[Input(name=cloud_custodian_release.name)],
                            run=Command(
                                user="root",
                                path="sh",
                                args=[
                                    "-exc",
                                    (
                                        "custodian run --region 'us-east-1'"
                                        " --output-dir '.'"
                                        f" '{cloud_custodian_release.name}/cloud_custodian/{filename}'"  # noqa: E501
                                    ),
                                ],
                            ),
                        ),
                    ),
                ],
            )
        )

    return Pipeline(resources=[build_schedule, cloud_custodian_release], jobs=job_list)


if __name__ == "__main__":
    import sys

    from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

    output = pipeline_json_with_user_data(
        custodian_pipeline(),
        user_data={
            "description": (
                "Runs Cloud Custodian policy jobs on a 24h schedule against the "
                "`cloudcustodian/c7n` container: sync-ec2-tags, "
                "tag/cleanup orphaned EBS volumes, and tag/cleanup stale Packer "
                "security groups."
            ),
            "team": "infrastructure",
            "category": "core-platform",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p misc-cloud-custodian -c definition.json")  # noqa: T201
