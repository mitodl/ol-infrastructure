# ruff: noqa: E501
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

run_optimization_pipeline = """\
set -euo pipefail

# TODO: Change this to just pass in env vars instead of writing out the json and yaml files.
# This will require some small tweaks to the pull_input_data.py script to read from env vars instead of requiring files.
mkdir /opt/gurobi
cat > "/opt/gurobi/gurobi.lic" <<EOF
LICENSEID=${LICENSEID}
WLSACCESSID=${WLSACCESSID}
WLSSECRET=${WLSSECRET}
EOF


cd /app
cat > "google-ads.json" <<EOF
${GOOGLE_ADS_JSON}
EOF
cat > "google-ads.yaml" <<EOF
json_key_file_path: google-ads.json
developer_token: ${GOOGLE_DEVELOPER_TOKEN}
use_proto_plus: True
login_customer_id: ${GOOGLE_MANAGER_ACCOUNT}
EOF

# Kind of gnarly, but this lets us store the customer ID in vault since it's a semi-sensitive value.
CUSTOMER_ID_FOR_COURSE=$(echo "${CUSTOMER_ID_FOR_COURSES}" | uv run python -c "import json,sys; d=json.load(sys.stdin); print(d['${COURSE_NAME}'])")

uv run python scripts/pull_input_data.py --google-ads-yaml=google-ads.yaml --customer-id=${CUSTOMER_ID_FOR_COURSE} --output-course=${COURSE_NAME} --datasets=ads_reports
uv run python scripts/run_pipeline.py --course ${COURSE_NAME}
"""


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
