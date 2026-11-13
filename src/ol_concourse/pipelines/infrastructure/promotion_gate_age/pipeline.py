"""Weekday report of Pulumi promotion gates nobody has closed.

Production on a chained Pulumi pipeline deploys only when a human closes the
`promotion-to-production` gate issue on github.mit.edu. An unclosed gate is
silent: every job stays green while production runs older code than QA. The
Dagster server gate opened 2026-08-21 sat for 2 days 21 hours that way.

`bin/promotion-gate-age` lists every open Pulumi gate older than 24 hours and
posts one Slack message naming them. Gates labelled `promotion-held` are
counted rather than listed, and it posts nothing when no unheld gate is
stale. A
Slack post and not a red build, because nobody watches this pipeline; same
reasoning as `alerting_drift`.

Credentials are the ones the gates already use: the infrastructure team's
`((github.issues_resource_access_token))` that every `github-issues` resource
in a Pulumi chain reads the gate repository with, and `((eks.slack_url))`, the
webhook the k8s_apps Pulumi chains report failures to.
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
from ol_concourse.lib.resources import git_repo, schedule

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

ol_infrastructure = git_repo(
    Identifier("ol-infrastructure"),
    uri="https://github.com/mitodl/ol-infrastructure",
    paths=["bin/promotion-gate-age"],
)

# 14:00 UTC is 09:00 or 10:00 in Boston, before most promotions get decided.
weekday_schedule = schedule(
    Identifier("weekday-morning"),
    interval="24h",
    start="14:00",
    stop="15:00",
    days=["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"],
)

ol_infrastructure_image = AnonymousResource(
    type=REGISTRY_IMAGE,
    source={
        "repository": dockerhub_ecr_image_uri("mitodl/ol-infrastructure"),
        "tag": "latest",
        "aws_region": ECR_REGION,
    },
)


def gate_age_job() -> Job:
    return Job(
        name=Identifier("report-stale-promotion-gates"),
        plan=[
            GetStep(get=weekday_schedule.name, trigger=True),
            GetStep(get=ol_infrastructure.name, trigger=False),
            TaskStep(
                task=Identifier("report-stale-promotion-gates"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=ol_infrastructure_image,
                    inputs=[Input(name=ol_infrastructure.name)],
                    params={
                        "GITHUB_TOKEN": "((github.issues_resource_access_token))",
                        "SLACK_WEBHOOK_URL": "((eks.slack_url))",
                    },
                    run=Command(
                        path="python",
                        args=["ol-infrastructure/bin/promotion-gate-age", "--post"],
                    ),
                ),
            ),
        ],
    )


def promotion_gate_age_pipeline() -> Pipeline:
    return Pipeline(
        resources=[weekday_schedule, ol_infrastructure],
        jobs=[gate_age_job()],
    )


if __name__ == "__main__":
    definition_json = pipeline_json_with_user_data(
        promotion_gate_age_pipeline(),
        user_data={
            "description": (
                "Weekday job that runs `bin/promotion-gate-age` to list Pulumi "
                "`promotion-to-production` gate issues open longer than 24 "
                "hours and posts them to Slack, so a production stack left "
                "behind QA is visible. It never closes a gate."
            ),
            "team": "infrastructure",
            "category": "core-platform",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(definition_json)
    sys.stdout.write(definition_json)
    print()  # noqa: T201
    print("fly -t pr-inf sp -p promotion-gate-age -c definition.json")  # noqa: T201
