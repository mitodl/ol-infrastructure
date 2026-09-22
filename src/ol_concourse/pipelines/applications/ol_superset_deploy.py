import sys

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
from ol_concourse.lib.resources import git_repo

from ol_concourse.pipelines.constants import ECR_REGION, dockerhub_ecr_image_uri

ol_data_platform_repo = git_repo(
    name=Identifier("ol-data-platform-repository"),
    uri="https://github.com/mitodl/ol-data-platform",
    branch="main",
    check_every="60s",
    paths=["src/ol_superset/assets/", "src/ol_superset/policies/"],
)

# ol-superset roles/RLS commands source ol_governance_roles.json from this repo,
# not ol-data-platform, so it has to be checked out alongside the assets.
ol_infrastructure_repo = git_repo(
    name=Identifier("ol-infrastructure-repository"),
    uri="https://github.com/mitodl/ol-infrastructure",
    branch="main",
    check_every="60s",
    paths=["src/ol_infrastructure/applications/superset/ol_governance_roles.json"],
)

# Shell script that writes ~/.sup/config.yml from injected Vault secrets, then
# promotes assets QA -> production and syncs governance roles and row-level
# access policies to production so a run never needs manual follow-up.
#
# Vault secret paths (KV v1, mount: secret-data):
#   superset_qa_service_account   -> superset_url, oauth_token_url, client_id, client_secret  # noqa: E501
#   superset_service_account      -> superset_url, oauth_token_url, client_id, client_secret  # noqa: E501
_deploy_script = """\
set -euo pipefail

mkdir -p "${HOME}/.sup"
cat > "${HOME}/.sup/config.yml" <<EOF
superset_instances:
  superset-qa:
    url: ${SUPERSET_QA_URL}
    auth_method: oauth
    oauth_token_url: ${SUPERSET_QA_OAUTH_TOKEN_URL}
    oauth_client_id: ${SUPERSET_QA_CLIENT_ID}
    oauth_client_secret: ${SUPERSET_QA_CLIENT_SECRET}
  superset-production:
    url: ${SUPERSET_PRODUCTION_URL}
    auth_method: oauth
    oauth_token_url: ${SUPERSET_PRODUCTION_OAUTH_TOKEN_URL}
    oauth_client_id: ${SUPERSET_PRODUCTION_CLIENT_ID}
    oauth_client_secret: ${SUPERSET_PRODUCTION_CLIENT_SECRET}
current_instance_name: superset-qa
EOF

ol-superset promote \\
    --force \\
    --skip-validation \\
    --assets-dir ol-data-platform-repository/src/ol_superset/assets

GOVERNANCE_JSON=ol-infrastructure-repository/src/ol_infrastructure/applications/superset/ol_governance_roles.json
RLS_POLICY_FILE=ol-data-platform-repository/src/ol_superset/policies/ol_rls_policies.json

ROLES_SYNC_LOG=$(mktemp)
ol-superset roles sync superset-production \\
    --yes \\
    --assets-dir ol-data-platform-repository/src/ol_superset/assets \\
    --governance-json "${GOVERNANCE_JSON}" | tee "${ROLES_SYNC_LOG}"

ol-superset apply-rls superset-production \\
    --yes \\
    --policy-file "${RLS_POLICY_FILE}"

# roles sync exits 0 even when a governance role doesn't exist yet in
# superset-production (e.g. it hasn't been imported by a pending Production
# Pulumi deploy). Fail loudly instead of leaving that role's permissions
# silently unsynced -- re-run this job once the role has been imported.
if grep -qF "Role not found in" "${ROLES_SYNC_LOG}"; then
    echo "One or more governance roles are missing from superset-production." >&2
    echo "Re-run this job after the Production Superset Pulumi deploy imports them." >&2
    exit 1
fi
"""

deploy_pipeline = Pipeline(
    resources=[ol_data_platform_repo, ol_infrastructure_repo],
    jobs=[
        Job(
            name=Identifier("deploy-superset-assets-to-production"),
            plan=[
                GetStep(get=ol_data_platform_repo.name, trigger=True),
                # Not a trigger: Concourse ORs multiple trigger=True get steps, so
                # triggering on this too would force-promote QA assets to production
                # on a governance-only change with no asset review involved.
                GetStep(get=ol_infrastructure_repo.name),
                TaskStep(
                    task=Identifier("promote-assets-and-sync-governance"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": dockerhub_ecr_image_uri(
                                    "mitodl/ol-superset"
                                ),
                                "tag": "latest",
                                "aws_region": ECR_REGION,
                            },
                        ),
                        inputs=[
                            Input(name=ol_data_platform_repo.name),
                            Input(name=ol_infrastructure_repo.name),
                        ],
                        params={
                            "SUPERSET_QA_URL": "((superset_qa_service_account.superset_url))",  # noqa: E501
                            "SUPERSET_QA_OAUTH_TOKEN_URL": "((superset_qa_service_account.oauth_token_url))",  # noqa: E501
                            "SUPERSET_QA_CLIENT_ID": "((superset_qa_service_account.client_id))",  # noqa: E501
                            "SUPERSET_QA_CLIENT_SECRET": "((superset_qa_service_account.client_secret))",  # noqa: E501
                            "SUPERSET_PRODUCTION_URL": "((superset_service_account.superset_url))",  # noqa: E501
                            "SUPERSET_PRODUCTION_OAUTH_TOKEN_URL": "((superset_service_account.oauth_token_url))",  # noqa: E501
                            "SUPERSET_PRODUCTION_CLIENT_ID": "((superset_service_account.client_id))",  # noqa: E501
                            "SUPERSET_PRODUCTION_CLIENT_SECRET": "((superset_service_account.client_secret))",  # noqa: E501
                        },
                        run=Command(
                            path="bash",
                            args=["-c", _deploy_script],
                        ),
                    ),
                ),
            ],
        )
    ],
)

if __name__ == "__main__":
    with open("definition.json", "w") as definition:  # noqa: PTH123
        definition.write(deploy_pipeline.model_dump_json(indent=2))
    sys.stdout.write(deploy_pipeline.model_dump_json(indent=2))
    sys.stdout.write(
        "\nfly -t <target> set-pipeline -p ol-superset-deploy -c definition.json\n"
    )
