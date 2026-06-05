#!/bin/bash
set -euo pipefail

working_dir=$(pwd)

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
uv run python scripts/run_pipeline.py --course ${COURSE_NAME} --time-limit="1800.0" 2>&1 | tee -a "optimization_pipeline.log"

# shellcheck disable=SC2086
uv run python scripts/push_output_data.py --google-ads-yaml=google-ads.yaml --customer-id=${CUSTOMER_ID_FOR_COURSE} --output-course=${COURSE_NAME} --datasets=budget --execute 2>&1 | tee -a "optimization_pipeline.log"

echo "Pipeline finished. Checking for warnings in the log..."

# Pipe warnings to an output. If that output exists, we want to emit a slack message about it.
# Messages emitted by our push script use "WARNING" over stdout.
warning_result=$(grep -A 3 "WARNING" optimization_pipeline.log || true)
echo "Warnings check complete."
if [ -n "$warning_result" ]; then
  echo "Warnings found in the log"
  echo "$warning_result" > "$working_dir"/optimization_pipeline_output/warnings.txt
fi
echo "Pipeline execution complete."
