# ruff: noqa: E501
from pathlib import Path

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    GetStep,
    Identifier,
    Input,
    Job,
    Output,
    Pipeline,
    Platform,
    TaskConfig,
    TaskStep,
)
from ol_concourse.lib.notifications import notification
from ol_concourse.lib.resource_types import (
    slack_notification_resource as slack_notification_resource_type,
)
from ol_concourse.lib.resources import schedule, slack_notification

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri
from ol_concourse.pipelines.pipeline_output import pipeline_json_with_user_data

COURSES = ["ml", "gen_ai", "sys_think", "sys_eng"]

run_optimization_pipeline = (
    Path(__file__).parent.joinpath("run_optimization_pipeline.sh").read_text()
)

check_for_warnings = Path(__file__).parent.joinpath("handle_warnings.sh").read_text()

slack_notification_resource = slack_notification(
    Identifier("slack-notification"), url="((google_ads_optimization.slack_url))"
)

ad_optimization_schedule = schedule(Identifier("optimization-schedule"), interval="24h")


def ad_optimization_pipeline() -> Pipeline:
    ad_optimization_object = Job(
        name=Identifier("ad-optimization"),
        plan=[
            GetStep(get=ad_optimization_schedule.name, trigger=True),
            TaskStep(
                attempts=3,
                task=Identifier("ad-optimization-pipeline"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source={
                            "repository": dockerhub_ecr_image_uri("mitodl/ad-opt"),
                            "aws_region": ECR_REGION,
                        },
                    ),
                    params={
                        "WLSACCESSID": "((google_ads_optimization.gurobi_wls_access_id))",
                        "WLSSECRET": "((google_ads_optimization.gurobi_wls_secret))",
                        "LICENSEID": "((google_ads_optimization.gurobi_wls_license_id))",
                        "GOOGLE_DEVELOPER_TOKEN": "((google_ads_optimization.google_developer_token))",
                        "GOOGLE_MANAGER_ACCOUNT": "((google_ads_optimization.google_manager_account))",
                        "GOOGLE_ADS_JSON": "((google_ads_optimization.google_ads_json))",
                        # This is a map of course name to customer ID.
                        "CUSTOMER_ID_FOR_COURSES": "((google_ads_optimization.customer_id_for_courses))",
                        "COURSE_NAME": "((course_name))",
                        "SEMRUSH_API_KEY": "((google_ads_optimization.semrush_api_key))",
                        "GRAFANA_USERNAME": "((grafana.metrics_write_user))",
                        "GRAFANA_TOKEN": "((grafana.metrics_write_token))",
                        "GRAFANA_URL": "((grafana.metrics_url))",
                    },
                    run=Command(
                        path="bash",
                        args=["-c", run_optimization_pipeline],
                    ),
                    outputs=[Output(name=Identifier("optimization_pipeline_output"))],
                ),
                on_error=notification(
                    slack_notification_resource,
                    "Google Ads Optimization Pipeline Error",
                    "Google Ads Optimization Pipeline errored for ((course_name)). Check the pipeline logs for details.",
                    alert_type="errored",
                ),
                on_failure=notification(
                    slack_notification_resource,
                    "Google Ads Optimization Pipeline Failure",
                    "Google Ads Optimization Pipeline failed for ((course_name)). Check the pipeline logs for details.",
                    alert_type="failed",
                ),
                on_abort=notification(
                    slack_notification_resource,
                    "Google Ads Optimization Pipeline Abort",
                    "Google Ads Optimization Pipeline aborted for ((course_name)). Check the pipeline logs for details.",
                    alert_type="aborted",
                ),
            ),
            TaskStep(
                task=Identifier("ad-optimization-pipeline-emit-warnings"),
                config=TaskConfig(
                    inputs=[Input(name=Identifier("optimization_pipeline_output"))],
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type="registry-image",
                        source={
                            "repository": dockerhub_ecr_image_uri("debian"),
                            "tag": "12-slim",
                            "aws_region": ECR_REGION,
                        },
                    ),
                    run=Command(
                        path="bash",
                        args=["-c", check_for_warnings],
                    ),
                ),
                on_failure=notification(
                    slack_notification_resource,
                    "Google Ads Optimization Warnings detected",
                    "Google Ads Optimization Pipeline emitted warnings for ((course_name)). Check the pipeline logs for details.",
                    alert_type="failed",
                ),
            ),
        ],
    )
    return Pipeline(
        jobs=[ad_optimization_object],
        resources=[slack_notification_resource, ad_optimization_schedule],
        resource_types=[slack_notification_resource_type()],
    )


if __name__ == "__main__":
    import sys

    output = pipeline_json_with_user_data(
        ad_optimization_pipeline(),
        user_data={
            "description": (
                "Runs the Google Ads bid-optimization job on a 24h schedule for "
                f"one course ({', '.join(COURSES)}), instanced via "
                "`--instance-var course_name=...`. Pulls Gurobi-solved bid "
                "recommendations from Google Ads + SEMrush data, applies them, "
                "and alerts to Slack on error/failure/abort/warning."
            ),
            "team": "main",
            "category": "apps-misc",
        },
    )
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(output)
    sys.stdout.write(output)
    print()  # noqa: T201
    for course in COURSES:
        sys.stdout.write(
            f"fly -t <prod_target> sp -p google-ads-optimization -c definition.json --instance-var course_name={course}\n"
        )
