# ruff: noqa: E501
from pathlib import Path

from ol_concourse.lib.constants import REGISTRY_IMAGE
from ol_concourse.lib.models.pipeline import (
    AnonymousResource,
    Command,
    Identifier,
    Job,
    Pipeline,
    Platform,
    RegistryImage,
    TaskConfig,
    TaskStep,
)

COURSES = ["ml", "gen_ai", "sys_think", "sys_eng"]

run_optimization_pipeline = (
    Path(__file__).parent.joinpath("run_optimization_pipeline.sh").read_text()
)


def ad_optimization_pipeline() -> Pipeline:
    ad_optimization_object = Job(
        name=Identifier("ad-optimization"),
        plan=[
            TaskStep(
                task=Identifier("ad-optimization-pipeline"),
                config=TaskConfig(
                    platform=Platform.linux,
                    image_resource=AnonymousResource(
                        type=REGISTRY_IMAGE,
                        source=RegistryImage(repository="mitodl/ad-opt"),
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
                    },
                    run=Command(
                        path="bash",
                        args=["-c", run_optimization_pipeline],
                    ),
                ),
            ),
        ],
    )
    return Pipeline(jobs=[ad_optimization_object])


if __name__ == "__main__":
    import sys

    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(ad_optimization_pipeline().model_dump_json(indent=2))
    sys.stdout.write(ad_optimization_pipeline().model_dump_json(indent=2))
    print()  # noqa: T201
    for course in COURSES:
        sys.stdout.write(
            f"fly -t <prod_target> sp -p google-ads-optimization -c definition.json --instance-var course_name={course}\n"
        )
