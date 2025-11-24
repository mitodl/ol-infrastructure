import dataclasses
import sys

from ol_concourse.lib.containers import container_build_task
from ol_concourse.lib.models.pipeline import (
    GetStep,
    Identifier,
    Input,
    Job,
    Pipeline,
    PutStep,
    Resource,
)
from ol_concourse.lib.resources import s3_object, ssh_git_repo


@dataclasses.dataclass
class CourseImageInfo:
    course_name: str
    image_name: str
    repo_uri: str | None = None
    s3_bucket: str | None = None
    s3_object_path: str | None = None


# Used to make a parameterized pipeline which builds
# and publishes the image from GH to ECR
courses = [
    CourseImageInfo(
        course_name="course_deep_learning_foundations_and_applications",
        repo_uri="git@github.mit.edu:ol-notebooks/course_deep_learning_foundations_and_applications.git",
        image_name="deep_learning_foundations_and_applications",
    ),
    CourseImageInfo(
        course_name="supervised_learning_fundamentals",
        repo_uri="git@github.mit.edu:ol-notebooks/course_supervised_learning_fundamentals.git",
        image_name="supervised_learning_fundamentals",
    ),
    CourseImageInfo(
        course_name="introduction_to_data_analytics_and_machine_learning",
        repo_uri="git@github.mit.edu:ol-notebooks/course_introduction_to_data_analytics_and_machine_learning.git",
        image_name="introduction_to_data_analytics_and_machine_learning",
    ),
    CourseImageInfo(
        course_name="clustering_and_descriptive_ai",
        repo_uri="git@github.mit.edu:ol-notebooks/course_clustering_and_descriptive_ai.git",
        image_name="clustering_and_descriptive_ai",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.intro",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.INTRO-2025_C604.git",
        image_name="uai_source-uai.intro",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.0",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.0-3T2025.git",
        image_name="uai_source-uai.0",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.0a",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.0A-3T2025.git",
        image_name="uai_source-uai.0a",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.st1",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.ST.1-1T2026.git",
        image_name="uai_source-uai.st1",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.1",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.1-2T2025.git",
        image_name="uai_source-uai.1",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.2",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.2-2T2025.git",
        image_name="uai_source-uai.2",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.3",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.3-3T2025.git",
        image_name="uai_source-uai.3",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.4",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.4-2025_C604.git",
        image_name="uai_source-uai.4",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.5",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.5-2025_C604.git",
        image_name="uai_source-uai.5",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.6",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.6-3T2025.git",
        image_name="uai_source-uai.6",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.7",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.7-3T2025.git",
        image_name="uai_source-uai.7",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.8",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.8-3T2025.git",
        image_name="uai_source-uai.8",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.9",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.9-3T2025.git",
        image_name="uai_source-uai.9",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.11",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.11-3T2025.git",
        image_name="uai_source-uai.11",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.12",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.12-3T2025.git",
        image_name="uai_source-uai.12",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.13",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.13-3T2025.git",
        image_name="uai_source-uai.13",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.mltl1",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.MLTL.1-1T2026",
        image_name="uai_source-uai.mltl1",
    ),
    CourseImageInfo(
        course_name="uai_source-uai.pm1",
        repo_uri="git@github.mit.edu:ol-notebooks/UAI_SOURCE-UAI.PM.1-1T2026",
        image_name="uai_source-uai.pm1",
    ),
]

# This infers the ECR url from the AWS account,
# the region and the repository name
# This part is common between both pipelines
course_image = Resource(
    name=Identifier("course_image"),
    type="registry-image",
    icon="docker",
    source={
        "repository": "ol-course-notebooks",
        "tag": "((image_name))",
        "aws_region": "us-east-1",
    },
)


def pipeline_for_github():
    course_repository = ssh_git_repo(
        name=Identifier("course_name"),
        uri="((course_repo))",
        branch="main",
        private_key="((github.ol_notebooks_private_ssh_key))",
    )

    build_task = container_build_task(
        inputs=[Input(name=course_repository.name)],
        build_parameters={"CONTEXT": course_repository.name},
    )

    return Pipeline(
        resources=[course_repository, course_image],
        jobs=[
            Job(
                name=Identifier("build-and-publish-container"),
                plan=[
                    GetStep(get=course_repository.name, trigger=True),
                    build_task,
                    PutStep(
                        put=course_image.name,
                        params={
                            "image": "image/image.tar",
                            "additional_tags": f"./{course_repository.name}"
                            f"/.git/describe_ref",
                        },
                    ),
                ],
            )
        ],
    )


def pipeline_for_s3():
    s3_archive = s3_object(
        name=Identifier("course_name"),
        bucket="((s3_bucket))",
        object_path="((s3_object_path))",
    )

    # We may want to remove the cruft that the S3 resource
    # provides alongside the unzipped archive:
    # s3_uri, url, version files as detailed at
    # https://github.com/concourse/s3-resource?tab=readme-ov-file#in-fetch-an-object-from-the-bucket

    build_task = container_build_task(
        inputs=[Input(name=s3_archive.name)],
        build_parameters={"CONTEXT": f"{s3_archive.name}"},
    )

    return Pipeline(
        resources=[s3_archive, course_image],
        jobs=[
            Job(
                name=Identifier("build-and-publish-container"),
                plan=[
                    GetStep(get=s3_archive.name, trigger=True, params={"unpack": True}),
                    build_task,
                    PutStep(
                        put=course_image.name,
                        params={
                            "image": "image/image.tar",
                        },
                    ),
                ],
            )
        ],
    )


if __name__ == "__main__":
    github_pipeline = pipeline_for_github()
    with open("github_definition.json", "w") as definition:  # noqa: PTH123
        definition.write(github_pipeline.model_dump_json(indent=2))
    sys.stdout.write(github_pipeline.model_dump_json(indent=2))
    s3_pipeline = pipeline_for_s3()
    with open("s3_definition.json", "w") as definition:  # noqa: PTH123
        definition.write(s3_pipeline.model_dump_json(indent=2))
    sys.stdout.write(s3_pipeline.model_dump_json(indent=2))
    for course in courses:
        if course.repo_uri:
            sys.stdout.write(
                f"fly -t <prod_target> set-pipeline "
                f"-p jupyter_notebook_docker_image_build "
                f"-c github_definition.json --var course_repo={course.repo_uri} "
                f"--instance-var image_name={course.image_name}\n"
            )
        else:
            sys.stdout.write(
                f"fly -t <prod_target> set-pipeline "
                f"-p jupyter_notebook_docker_image_build "
                f"-c s3_definition.json --var s3_bucket={course.s3_bucket} "
                f"--var s3_object_path={course.s3_object_path} "
                f"--instance-var image_name={course.image_name}\n"
            )
