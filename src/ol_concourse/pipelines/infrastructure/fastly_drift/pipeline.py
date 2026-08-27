"""Nightly comparison of Pulumi's Fastly state against what Fastly is serving.

`bin/fastly-drift-audit audit` reads every stack checkpoint out of
`s3://mitol-pulumi-state`, pulls the child-object names each `ServiceVcl`
records, and diffs them against the version Fastly reports as active. A name
that state claims exists and the live service does not have fails this job.

That gap is why hq#12449 ran for days: an illegal `/` in a VCL snippet name was
rejected by the Fastly API mid-apply, the failed run persisted the desired
snippet set into state anyway, and the provider's `SetDiff` then read the
never-created snippet back as `Unmodified`. No later `pulumi up` could heal it,
and nothing was comparing the two sides -- every Fastly-bearing stack runs
`refresh_stack=False`, set *because* the stack has Fastly resources.

Unlike `iam_drift` and `github_drift` this job opens no pull request. There is
no code change to propose: the declared configuration is already correct and
it is the state and the live service that disagree. The remedy is a targeted
`pulumi refresh` + `pulumi up`, so a failure notifies Slack with a link to that
runbook instead.

Needs no new credentials. The Production `infra` pool already grants the
`s3:GetObject*`/`s3:ListBucket*` this reads with (the `pulumi_state` policy
module) and the `kms:Decrypt` that the SOPS read of `fastly.yaml` needs (the
`infra` module). The audit authenticates to Fastly with the read-only
`global_read_api_key`, never the admin token, and issues only GETs -- it is
structurally incapable of changing either Fastly or Pulumi state.
"""

import sys

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
from ol_concourse.lib.notifications import notification
from ol_concourse.lib.resource_types import (
    slack_notification_resource as slack_notification_resource_type,
)
from ol_concourse.lib.resources import git_repo, schedule, slack_notification

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

AWS_REGION = "us-east-1"
GITHUB_REPOSITORY = "mitodl/ol-infrastructure"
RUNBOOK_URL = (
    "https://engineering.ol.mit.edu/platform_services/cloud_infrastructure/"
    "fastly_pulumi_state_drift/"
)

ol_infrastructure = git_repo(
    Identifier("ol-infrastructure"),
    uri=f"https://github.com/{GITHUB_REPOSITORY}",
)

drift_schedule = schedule(
    Identifier("nightly-schedule"),
    interval="24h",
    start="02:00",
    stop="03:00",
)

ol_infrastructure_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source={
        "repository": dockerhub_ecr_image_uri("mitodl/ol-infrastructure"),
        "tag": "latest",
        "aws_region": ECR_REGION,
    },
)

slack_notification_resource = slack_notification(
    Identifier("slack-notification"), url="((eks.slack_url))"
)


def drift_job() -> Job:
    """Build the nightly audit job."""
    return Job(
        name=Identifier("check-fastly-drift"),
        plan=[
            GetStep(get=drift_schedule.name, trigger=True),
            GetStep(get=ol_infrastructure.name, trigger=False),
            TaskStep(
                task=Identifier("audit-fastly-drift"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=ol_infrastructure_image,
                    inputs=[Input(name=ol_infrastructure.name)],
                    params={
                        # The checkout, not the image's installed copy: the
                        # audit derives its scope from the Pulumi.yaml files
                        # sitting next to the script it runs.
                        "PYTHONPATH": f"{ol_infrastructure.name}/src",
                        "AWS_DEFAULT_REGION": AWS_REGION,
                    },
                    run=Command(
                        user="root",
                        path="sh",
                        args=[
                            "-exc",
                            (
                                f"python {ol_infrastructure.name}/bin/"
                                "fastly-drift-audit audit"
                                f" --repo-root {ol_infrastructure.name}"
                            ),
                        ],
                    ),
                ),
                on_failure=notification(
                    slack_notification_resource,
                    "Fastly drift detected",
                    (
                        "Pulumi state claims a Fastly object exists that the live"
                        " service does not have -- the hq#12449 failure mode. No"
                        " `pulumi up` will heal this on its own. Job output lists the"
                        f" affected services; repair runbook: {RUNBOOK_URL}"
                    ),
                    alert_type="failed",
                ),
                on_error=notification(
                    slack_notification_resource,
                    "Fastly drift audit errored",
                    (
                        "The nightly Fastly drift audit failed to run, so the estate"
                        " is currently unchecked rather than clean. Check the job"
                        " output."
                    ),
                    alert_type="errored",
                ),
            ),
        ],
    )


def fastly_drift_pipeline() -> Pipeline:
    """Assemble the pipeline."""
    return Pipeline(
        resources=[drift_schedule, ol_infrastructure, slack_notification_resource],
        resource_types=[slack_notification_resource_type()],
        jobs=[drift_job()],
    )


if __name__ == "__main__":
    definition_json = fastly_drift_pipeline().model_dump_json(indent=2)
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(definition_json)
    sys.stdout.write(definition_json)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p fastly-drift -c definition.json")  # noqa: T201
