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
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resources import git_repo, schedule

cloud_custodian_release = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
)

build_schedule = schedule(Identifier("build-schedule"), "24h")

custodian_registry_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source=RegistryImage(repository="cloudcustodian/c7n", tag="0.9.15.0"),
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

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(custodian_pipeline().model_dump_json(indent=2))
    sys.stdout.write(custodian_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    print("fly -t pr-inf sp -p misc-cloud-custodian -c definition.json")  # noqa: T201
