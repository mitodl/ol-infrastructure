from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Job,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.resource_types import github_issues_resource
from ol_concourse.lib.resources import github_issues

"""
resources:
  - name: my-concourse-github-issues
    type: concourse-github-issues
    source:
      github_auth_token: ((github.auth_token))
      project_key: concourse
      repo: "mitodl/my-project"
      issue_prefix: "CONCOURSE_WORKFLOW"

"""


def hello_pipeline() -> Pipeline:
    test_pipeline_issues = github_issues(
        name=Identifier("test_pipeline_issues"),
        repository="mitodl/concourse-workflow",
        issue_prefix="CONCOURSE",
    )
    hello_job_object = Job(
        name=Identifier("run-github-issue-integ"),
        max_in_flight=1,  # Only allow 1 Pulumi task at a time since they lock anyway.
        plan=[
            GetStep(get=test_pipeline_issues.name, trigger=True),
            TaskStep(
                task=Identifier("github-issues-task"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="busybox"),
                    ),
                    run=Command(path="echo", args=["Hello, World!"]),
                ),
            ),
        ],
    )
    return Pipeline(
        resource_types=[github_issues_resource()],
        resources=[test_pipeline_issues],
        jobs=[hello_job_object],
    )


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(hello_pipeline().model_dump_json(indent=2))
    sys.stdout.write(hello_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    print(  # noqa: T201
        "fly -t pr-inf sp -p test-github-issues-integ -c definition.json"
    )
