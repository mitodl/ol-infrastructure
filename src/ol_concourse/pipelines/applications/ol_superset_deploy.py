import sys  # noqa: INP001

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

ol_data_platform_repo = git_repo(
    name=Identifier("ol-data-platform-repository"),
    uri="https://github.com/mitodl/ol-data-platform",
    branch="main",
    check_every="60s",
    paths=["src/ol_superset/assets/"],
)

# Shell script that writes ~/.sup/config.yml from injected Vault secrets and
# then runs ol-superset promote --force to deploy assets to production.
#
# Vault secret paths (KV v1, mount: secret-data):
#   superset_qa_service_account   -> superset_url, oauth_token_url, client_id, client_secret, oauth_username, oauth_password  # noqa: E501
#   superset_service_account      -> superset_url, oauth_token_url, client_id, client_secret, oauth_username, oauth_password  # noqa: E501
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
    oauth_username: ${SUPERSET_QA_OAUTH_USERNAME}
    oauth_password: ${SUPERSET_QA_OAUTH_PASSWORD}
  superset-production:
    url: ${SUPERSET_PRODUCTION_URL}
    auth_method: oauth
    oauth_token_url: ${SUPERSET_PRODUCTION_OAUTH_TOKEN_URL}
    oauth_client_id: ${SUPERSET_PRODUCTION_CLIENT_ID}
    oauth_client_secret: ${SUPERSET_PRODUCTION_CLIENT_SECRET}
    oauth_username: ${SUPERSET_PRODUCTION_OAUTH_USERNAME}
    oauth_password: ${SUPERSET_PRODUCTION_OAUTH_PASSWORD}
current_instance_name: superset-qa
EOF

ol-superset promote \\
    --force \\
    --skip-validation \\
    --assets-dir ol-data-platform-repository/src/ol_superset/assets
"""

deploy_pipeline = Pipeline(
    resources=[ol_data_platform_repo],
    jobs=[
        Job(
            name=Identifier("deploy-superset-assets-to-production"),
            plan=[
                GetStep(get=ol_data_platform_repo.name, trigger=True),
                TaskStep(
                    task=Identifier("promote-assets-to-production"),
                    config=TaskConfig(
                        platform=Platform.linux,
                        image_resource=AnonymousResource(
                            type="registry-image",
                            source={
                                "repository": "mitodl/ol-superset",
                                "tag": "latest",
                            },
                        ),
                        inputs=[Input(name=ol_data_platform_repo.name)],
                        params={
                            "SUPERSET_QA_URL": "((superset_qa_service_account.superset_url))",  # noqa: E501
                            "SUPERSET_QA_OAUTH_TOKEN_URL": "((superset_qa_service_account.oauth_token_url))",  # noqa: E501
                            "SUPERSET_QA_CLIENT_ID": "((superset_qa_service_account.client_id))",  # noqa: E501
                            "SUPERSET_QA_CLIENT_SECRET": "((superset_qa_service_account.client_secret))",  # noqa: E501
                            "SUPERSET_QA_OAUTH_USERNAME": "((superset_qa_service_account.oauth_username))",  # noqa: E501
                            "SUPERSET_QA_OAUTH_PASSWORD": "((superset_qa_service_account.oauth_password))",  # noqa: E501
                            "SUPERSET_PRODUCTION_URL": "((superset_service_account.superset_url))",  # noqa: E501
                            "SUPERSET_PRODUCTION_OAUTH_TOKEN_URL": "((superset_service_account.oauth_token_url))",  # noqa: E501
                            "SUPERSET_PRODUCTION_CLIENT_ID": "((superset_service_account.client_id))",  # noqa: E501
                            "SUPERSET_PRODUCTION_CLIENT_SECRET": "((superset_service_account.client_secret))",  # noqa: E501
                            "SUPERSET_PRODUCTION_OAUTH_USERNAME": "((superset_service_account.oauth_username))",  # noqa: E501
                            "SUPERSET_PRODUCTION_OAUTH_PASSWORD": "((superset_service_account.oauth_password))",  # noqa: E501
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
