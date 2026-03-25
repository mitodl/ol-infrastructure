#!/bin/bash
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
# shellcheck disable=SC2153
CUSTOMER_ID_FOR_COURSE=$(echo "${CUSTOMER_ID_FOR_COURSES}" | uv run python -c "import json,sys; d=json.load(sys.stdin); print(d['${COURSE_NAME}'])")

# shellcheck disable=SC2086
uv run python scripts/pull_input_data.py --google-ads-yaml=google-ads.yaml --customer-id=${CUSTOMER_ID_FOR_COURSE} --output-course=${COURSE_NAME} --datasets=ads_reports
# shellcheck disable=SC2086
uv run python scripts/run_pipeline.py --course ${COURSE_NAME}
