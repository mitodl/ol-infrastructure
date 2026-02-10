# ruff: noqa: E501

import pulumi_vault as vault
import pulumiverse_heroku as heroku
from pulumi import (
    ROOT_STACK_RESOURCE,
    Alias,
    Config,
    InvokeOptions,
    ResourceOptions,
    export,
)
from pulumi.output import Output
from pulumi_aws import iam, s3
from pulumi_vault import aws

from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info, skip_child_token=True)
setup_heroku_provider()

mit_open_config = Config("mit_open")
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")
aws_config = AWSBase(
    tags={
        "OU": "mit-open",
        "Environment": stack_info.env_suffix,
        "Application": "mit-open",
    }
)
app_env_suffix = {"ci": "ci", "qa": "rc", "production": "production"}[
    stack_info.env_suffix
]

app_storage_bucket_name = f"mit-open-app-storage-{app_env_suffix}"

application_storage_bucket_config = S3BucketConfig(
    bucket_name=app_storage_bucket_name,
    versioning_enabled=True,
    ownership_controls="BucketOwnerEnforced",
    tags=aws_config.tags,
)

application_storage_bucket = OLBucket(
    f"mit_open_learning_application_storage_bucket_{stack_info.env_suffix}",
    config=application_storage_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"mit_open_learning_application_storage_bucket_{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

FIVE_MINUTES = 300

course_data_bucket_config = S3BucketConfig(
    bucket_name=f"open-learning-course-data-{app_env_suffix}",
    versioning_enabled=True,
    ownership_controls="BucketOwnerEnforced",
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET"],
            allowed_headers=["*"],
            allowed_origins=["*"],
            max_age_seconds=FIVE_MINUTES,
        )
    ],
    tags=aws_config.tags,
)

course_data_bucket = OLBucket(
    f"mit-open-learning-course-data-{stack_info.env_suffix}",
    config=course_data_bucket_config,
    opts=ResourceOptions(
        aliases=[
            Alias(
                name=f"mit-open-learning-course-data-{stack_info.env_suffix}",
                parent=ROOT_STACK_RESOURCE,
            ),
        ]
    ),
)

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "RESOURCE_EFFECTIVELY_STAR": {},
}

s3_bucket_permissions = [
    {
        "Action": [
            "s3:GetObject*",
            "s3:ListBucket*",
            "s3:PutObject",
            "s3:PutObjectAcl",
            "S3:DeleteObject",
        ],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::odl-discussions-{app_env_suffix}",
            f"arn:aws:s3:::odl-discussions-{app_env_suffix}/*",
            f"arn:aws:s3:::{app_storage_bucket_name}",
            f"arn:aws:s3:::{app_storage_bucket_name}/*",
            f"arn:aws:s3:::open-learning-course-data-{app_env_suffix}",
            f"arn:aws:s3:::open-learning-course-data-{app_env_suffix}/*",
        ],
    },
    {
        "Action": ["s3:GetObject*", "s3:ListBucket*"],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::edxorg-{stack_info.env_suffix}-edxapp-courses",
            f"arn:aws:s3:::edxorg-{stack_info.env_suffix}-edxapp-courses/*",
            "arn:aws:s3:::mitx-etl-xpro-production-mitxpro-production",
            "arn:aws:s3:::mitx-etl-xpro-production-mitxpro-production/*",
            "arn:aws:s3:::mitx-etl-mitxonline-production",
            "arn:aws:s3:::mitx-etl-mitxonline-production/*",
            "arn:aws:s3:::ol-olx-course-exports",
            "arn:aws:s3:::ol-olx-course-exports/*",
            "arn:aws:s3:::ocw-content-storage",
            "arn:aws:s3:::ocw-content-storage/*",
            f"arn:aws:s3:::ol-ocw-studio-app-{app_env_suffix}",
        ],
    },
]

athena_warehouse_access_statements = [
    {
        "Effect": "Allow",
        "Action": [
            "s3:GetObject*",
            "s3:ListBucket",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-data-lake-mitx-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-data-lake-mitx-{stack_info.env_suffix}/*",
            f"arn:aws:s3:::ol-data-lake-mit-open-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-data-lake-mit-open-{stack_info.env_suffix}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:PutObject",
            "s3:GetBucketLocation",
            "s3:GetObject",
            "s3:ListBucket",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-warehouse-results-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-warehouse-results-{stack_info.env_suffix}/*",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "athena:ListDataCatalogs",
            "athena:ListWorkGroups",
        ],
        "Resource": ["*"],
    },
    {
        "Effect": "Allow",
        "Action": [
            "s3:ListBucket",
            "S3:GetObject",
        ],
        "Resource": [
            f"arn:aws:s3:::ol-data-lake-mitx-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-data-lake-mitx-{stack_info.env_suffix}/*",
            f"arn:aws:s3:::ol-data-lake-mit-open-{stack_info.env_suffix}",
            f"arn:aws:s3:::ol-data-lake-mit-open-{stack_info.env_suffix}/*",
            f"arn:aws:s3:::ol-warehouse-results-{stack_info.env_suffix}",
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "athena:BatchGetNamedQuery",
            "athena:BatchGetQueryExecution",
            "athena:GetNamedQuery",
            "athena:GetQueryExecution",
            "athena:GetQueryResults",
            "athena:GetQueryResultsStream",
            "athena:GetWorkGroup",
            "athena:ListNamedQueries",
            "athena:ListQueryExecutions",
            "athena:StartQueryExecution",
            "athena:StopQueryExecution",
        ],
        "Resource": [
            f"arn:*:athena:*:*:workgroup/ol-warehouse-{stack_info.env_suffix}"
        ],
    },
    {
        "Effect": "Allow",
        "Action": [
            "athena:GetDataCatalog",
            "athena:GetDatabase",
            "athena:GetTableMetadata",
            "athena:ListDatabases",
            "athena:ListTableMetadata",
        ],
        "Resource": ["arn:*:athena:*:*:datacatalog/*"],
    },
    {
        "Effect": "Allow",
        "Action": [
            "glue:BatchGetPartition",
            "glue:GetDatabase",
            "glue:GetDatabases",
            "glue:GetPartition",
            "glue:GetPartitions",
            "glue:GetTable",
            "glue:GetTables",
        ],
        "Resource": [
            "arn:aws:glue:*:*:catalog",
            f"arn:aws:glue:*:*:database/ol_warehouse_mitx_{stack_info.env_suffix}",
            f"arn:aws:glue:*:*:database/ol_warehouse_mit_open_{stack_info.env_suffix}",
            f"arn:aws:glue:*:*:table/*{stack_info.env_suffix}/*",
        ],
    },
]
open_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": s3_bucket_permissions + athena_warehouse_access_statements,
}

mit_open_iam_policy = iam.Policy(
    f"mit_open_iam_permissions_{stack_info.env_suffix}",
    name=f"mit-open-application-permissions-{stack_info.env_suffix}",
    path=f"/ol-applications/mit-open/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        open_policy_document, stringify=True, parliament_config=parliament_config
    ),
)

mit_open_vault_iam_role = aws.SecretBackendRole(
    f"mit-open-iam-permissions-vault-policy-{stack_info.env_suffix}",
    name=f"mit-open-application-{stack_info.env_suffix}",
    # TODO: Make this configurable to support multiple AWS backends. TMM 2021-03-04  # noqa: FIX002, TD002
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[mit_open_iam_policy.arn],
)

env_name = (
    "rc" if stack_info.env_suffix.lower() == "qa" else stack_info.env_suffix.lower()
)
vault_env_path = (
    "rc-apps" if stack_info.env_suffix.lower() != "production" else "production-apps"
)

heroku_vars = {
    "AKISMET_BLOG_URL": "https://discussions-rc.odl.mit.edu",
    "ALLOWED_HOSTS": '["*"]',
    "CORS_ALLOWED_ORIGIN_REGEXES": "['^.+ocw-next.netlify.app$']",
    "CSAIL_BASE_URL": "https://cap.csail.mit.edu/",
    "DUPLICATE_COURSES_URL": "https://raw.githubusercontent.com/mitodl/open-resource-blacklists/master/duplicate_courses.yml",
    "EDX_API_ACCESS_TOKEN_URL": "https://api.edx.org/oauth2/v1/access_token",
    "EDX_API_URL": "https://api.edx.org/catalog/v1/catalogs/10/courses",
    "ELASTICSEARCH_DEFAULT_TIMEOUT": "30",
    "ELASTICSEARCH_INDEXING_CHUNK_SIZE": "75",
    "FEATURE_ANONYMOUS_ACCESS": "True",
    "FEATURE_ARTICLE_UI": "True",
    "FEATURE_COMMENT_NOTIFICATIONS": "True",
    "FEATURE_COURSE_FILE_SEARCH": "True",
    "FEATURE_INDEX_UPDATES": "True",
    "FEATURE_MOIRA": "True",
    "FEATURE_PODCAST_APIS": "True",
    "FEATURE_PODCAST_SEARCH": "True",
    "FEATURE_PROFILE_UI": "True",
    "FEATURE_SAML_AUTH": "True",
    "FEATURE_SEARCH_UI": "True",
    "FEATURE_SPAM_EXEMPTIONS": "True",
    "FEATURE_USE_NEW_BRANDING": "True",
    "FEATURE_WIDGETS_UI": "True",
    "MITPE_BASE_URL": "https://professional.mit.edu/",
    "MITX_ONLINE_BASE_URL": "https://mitxonline.mit.edu/",
    "MITX_ONLINE_COURSES_API_URL": "https://mitxonline.mit.edu/api/courses/",
    "MITX_ONLINE_LEARNING_COURSE_BUCKET_NAME": "mitx-etl-mitxonline-production",
    "MITX_ONLINE_PROGRAMS_API_URL": "https://mitxonline.mit.edu/api/programs/",
    "NEW_RELIC_LOG": "stdout",
    "NODE_MODULES_CACHE": "False",
    "OCW_CONTENT_BUCKET_NAME": "ocw-content-storage",
    "OLL_ALT_URL": "https://openlearninglibrary.mit.edu/courses/",
    "OLL_API_ACCESS_TOKEN_URL": "https://openlearninglibrary.mit.edu/oauth2/access_token/",
    "OLL_API_URL": "https://discovery.openlearninglibrary.mit.edu/api/v1/catalogs/1/courses/",
    "OLL_BASE_URL": "https://openlearninglibrary.mit.edu/course/",
    "OPENSEARCH_DEFAULT_TIMEOUT": "30",
    "OPENSEARCH_INDEXING_CHUNK_SIZE": "75",
    "OPEN_DISCUSSIONS_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "OPEN_DISCUSSIONS_DB_CONN_MAX_AGE": "0",
    "OPEN_DISCUSSIONS_DB_DISABLE_SSL": "True",
    "OPEN_DISCUSSIONS_DEFAULT_SITE_KEY": "micromasters",
    "OPEN_DISCUSSIONS_EMAIL_PORT": "587",
    "OPEN_DISCUSSIONS_EMAIL_TLS": "True",
    "OPEN_DISCUSSIONS_FROM_EMAIL": "MIT ODL Discussions <odl-discussions-support@mit.edu>",
    "OPEN_DISCUSSIONS_FRONTPAGE_DIGEST_MAX_POSTS": "10",
    "OPEN_DISCUSSIONS_REDDIT_VALIDATE_SSL": "True",
    "OPEN_DISCUSSIONS_USE_S3": "True",
    "PROLEARN_CATALOG_API_URL": "https://prolearn.mit.edu/graphql",
    "SEE_BASE_URL": "https://executive.mit.edu/",
    "SOCIAL_AUTH_OL_OIDC_KEY": "ol-open-discussions-client",
    "SOCIAL_AUTH_SAML_CONTACT_NAME": "ODL Support",
    "SOCIAL_AUTH_SAML_IDP_ATTRIBUTE_EMAIL": "urn:oid:0.9.2342.19200300.100.1.3",
    "SOCIAL_AUTH_SAML_IDP_ATTRIBUTE_NAME": "urn:oid:2.16.840.1.113730.3.1.241",
    "SOCIAL_AUTH_SAML_IDP_ATTRIBUTE_PERM_ID": "urn:oid:1.3.6.1.4.1.5923.1.1.1.6",
    "SOCIAL_AUTH_SAML_IDP_ENTITY_ID": "https://idp.mit.edu/shibboleth",
    "SOCIAL_AUTH_SAML_IDP_URL": "https://idp.mit.edu/idp/profile/SAML2/Redirect/SSO",
    "SOCIAL_AUTH_SAML_ORG_DISPLAYNAME": "MIT Office of Digital Learning",
    "SOCIAL_AUTH_SAML_SECURITY_ENCRYPTED": "True",
    "USE_X_FORWARDED_HOST": "True",
    "USE_X_FORWARDED_PORT": "True",
    "YOUTUBE_FETCH_TRANSCRIPT_SCHEDULE_SECONDS": "21600",
    "YOUTUBE_FETCH_TRANSCRIPT_SLEEP_SECONDS": "20",
}
heroku_vars.update(**heroku_app_config.get_object("vars"))


# auth/aws-mitx
auth_aws_mitx_creds_mit_open_application_env = vault.generic.get_secret_output(
    path=f"aws-mitx/creds/mit-open-application-{stack_info.env_suffix.lower()}",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)

# secret-operations
secret_global_mailgun_api_key = vault.generic.get_secret_output(
    path="secret-global/mailgun",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_operations_global_mit_smtp = vault.generic.get_secret_output(
    path="secret-operations/global/mit-smtp",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_operations_global_mit_open_sentry_dsn = vault.generic.get_secret_output(
    path="secret-operations/global/mit-open/sentry-dsn",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_operations_sso_open_discussions = vault.generic.get_secret_output(
    path="secret-operations/sso/open-discussions",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_operations_env_mit_open_algolia = vault.generic.get_secret_output(
    path=f"secret-operations/{vault_env_path}/mit-open/algolia",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_operations_env_mit_open_embedly_key = vault.generic.get_secret_output(
    path=f"secret-operations/{vault_env_path}/mit-open/embedly_key",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_operations_env_mit_open_recaptcha_keys = vault.generic.get_secret_output(
    path=f"secret-operations/{vault_env_path}/mit-open/recaptcha-keys",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_operations_env_mit_open_saml = vault.generic.get_secret_output(
    path=f"secret-operations/{vault_env_path}/mit-open/saml",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_operations_env_tika_access_token = vault.generic.get_secret_output(
    path=f"secret-operations/{vault_env_path}/tika/access-token",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)

# secret-mit-open/global
secret_mit_open_global_akismet = vault.generic.get_secret_output(
    path="secret-mit-open/global/akismet",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_global_odlbot_github_access_token = vault.generic.get_secret_output(
    path="secret-mit-open/global/odlbot-github-access-token",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_global_mit_application_certificate = vault.generic.get_secret_output(
    path="secret-mit-open/global/mit-application-certificate",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_global_update_search_data_webhook_key = vault.generic.get_secret_output(
    path="secret-mit-open/global/update-search-data-webhook-key",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)

# secret-mit-open/<env>
secret_mit_open_env_elasticsearch = vault.generic.get_secret_output(
    path=f"secret-mit-open/{env_name}/elasticsearch",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_env_jwt_secret = vault.generic.get_secret_output(
    path=f"secret-mit-open/{env_name}/jwt_secret",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_env_open_discussions_reddit = vault.generic.get_secret_output(
    path=f"secret-mit-open/{env_name}/open-discussions-reddit",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_env_open_learning_library_client = vault.generic.get_secret_output(
    path=f"secret-mit-open/{env_name}/open-learning-library-client",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_env_django_secret_key = vault.generic.get_secret_output(
    path=f"secret-mit-open/{env_name}/django-secret-key",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_env_django_status_token = vault.generic.get_secret_output(
    path=f"secret-mit-open/{env_name}/django-status-token",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_env_youtube_developer_key = vault.generic.get_secret_output(
    path=f"secret-mit-open/{env_name}/youtube-developer-key",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)

# secret-mit-open/<vault_env_path>
secret_mit_open_env_ckeditor = vault.generic.get_secret_output(
    path=f"secret-mit-open/{vault_env_path}/ckeditor",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)
secret_mit_open_env_edx_api_client = vault.generic.get_secret_output(
    path=f"secret-mit-open/{vault_env_path}/edx-api-client",
    opts=InvokeOptions(parent=mit_open_vault_iam_role),
)

sensitive_heroku_vars = {
    "AWS_ACCESS_KEY_ID": auth_aws_mitx_creds_mit_open_application_env.data.apply(
        lambda data: "{}".format(data["access_key"])
    ),
    "AWS_SECRET_ACCESS_KEY": auth_aws_mitx_creds_mit_open_application_env.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "AKISMET_API_KEY": secret_mit_open_global_akismet.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
    "ALGOLIA_API_KEY": secret_operations_env_mit_open_algolia.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
    "ALGOLIA_APP_ID": secret_operations_env_mit_open_algolia.data.apply(
        lambda data: "{}".format(data["app_id"])
    ),
    "CKEDITOR_ENVIRONMENT_ID": secret_mit_open_env_ckeditor.data.apply(
        lambda data: "{}".format(data["environment_id"])
    ),
    "CKEDITOR_SECRET_KEY": secret_mit_open_env_ckeditor.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "CKEDITOR_UPLOAD_URL": secret_mit_open_env_ckeditor.data.apply(
        lambda data: "{}".format(data["upload_url"])
    ),
    "EDX_API_CLIENT_ID": secret_mit_open_env_edx_api_client.data.apply(
        lambda data: "{}".format(data["id"])
    ),
    "EDX_API_CLIENT_SECRET": secret_mit_open_env_edx_api_client.data.apply(
        lambda data: "{}".format(data["secret"])
    ),
    "EMBEDLY_KEY": secret_operations_env_mit_open_embedly_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "GITHUB_ACCESS_TOKEN": secret_mit_open_global_odlbot_github_access_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "MAILGUN_KEY": secret_global_mailgun_api_key.data.apply(
        lambda data: "{}".format(data["api_key"])
    ),
    "MIT_WS_CERTIFICATE": secret_mit_open_global_mit_application_certificate.data.apply(
        lambda data: "{}".format(data["certificate"])
    ),
    "MIT_WS_PRIVATE_KEY": secret_mit_open_global_mit_application_certificate.data.apply(
        lambda data: "{}".format(data["private_key"])
    ),
    "OCW_NEXT_SEARCH_WEBHOOK_KEY": secret_mit_open_global_update_search_data_webhook_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "OLL_API_CLIENT_ID": secret_mit_open_env_open_learning_library_client.data.apply(
        lambda data: "{}".format(data["client-id"])
    ),
    "OLL_API_CLIENT_SECRET": secret_mit_open_env_open_learning_library_client.data.apply(
        lambda data: "{}".format(data["client-secret"])
    ),
    "OPENSEARCH_HTTP_AUTH": secret_mit_open_env_elasticsearch.data.apply(
        lambda data: "{}".format(data["http_auth"])
    ),
    "OPEN_DISCUSSIONS_EMAIL_HOST": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_host"])
    ),
    "OPEN_DISCUSSIONS_EMAIL_PASSWORD": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_password"])
    ),
    "OPEN_DISCUSSIONS_EMAIL_USER": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_username"])
    ),
    "OPEN_DISCUSSIONS_JWT_SECRET": secret_mit_open_env_jwt_secret.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "OPEN_DISCUSSIONS_REDDIT_CLIENT_ID": secret_mit_open_env_open_discussions_reddit.data.apply(
        lambda data: "{}".format(data["reddit_client_id"])
    ),
    "OPEN_DISCUSSIONS_REDDIT_SECRET": secret_mit_open_env_open_discussions_reddit.data.apply(
        lambda data: "{}".format(data["reddit_secret"])
    ),
    "OPEN_DISCUSSIONS_REDDIT_URL": secret_mit_open_env_open_discussions_reddit.data.apply(
        lambda data: "{}".format(data["reddit_url"])
    ),
    "RECAPTCHA_SECRET_KEY": secret_operations_env_mit_open_recaptcha_keys.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "RECAPTCHA_SITE_KEY": secret_operations_env_mit_open_recaptcha_keys.data.apply(
        lambda data: "{}".format(data["site_key"])
    ),
    "SECRET_KEY": secret_mit_open_env_django_secret_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "SENTRY_DSN": secret_operations_global_mit_open_sentry_dsn.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "SOCIAL_AUTH_OL_OIDC_SECRET": secret_operations_sso_open_discussions.data.apply(
        lambda data: "{}".format(data["client_secret"])
    ),
    "SOCIAL_AUTH_SAML_IDP_X509": secret_operations_env_mit_open_saml.data.apply(
        lambda data: "{}".format(data["idp_x509"])
    ),
    "SOCIAL_AUTH_SAML_SP_PRIVATE_KEY": secret_operations_env_mit_open_saml.data.apply(
        lambda data: "{}".format(data["private_key"])
    ),
    "SOCIAL_AUTH_SAML_SP_PUBLIC_CERT": secret_operations_env_mit_open_saml.data.apply(
        lambda data: "{}".format(data["public_cert"])
    ),
    "STATUS_TOKEN": secret_mit_open_env_django_status_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "TIKA_ACCESS_TOKEN": secret_operations_env_tika_access_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "YOUTUBE_DEVELOPER_KEY": secret_mit_open_env_youtube_developer_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
}

if env_name != "ci":
    auth_postgres_env_opendiscussions = vault.generic.get_secret_output(
        path=f"postgres-{vault_env_path}-opendiscussions/creds/opendiscussions",
        with_lease_start_time=False,
        opts=InvokeOptions(parent=mit_open_vault_iam_role),
    )
    sensitive_heroku_vars["DATABASE_URL"] = (
        auth_postgres_env_opendiscussions.data.apply(
            lambda data: (
                "postgres://{}:{}@{}-rds-postgresql-opendiscussions.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/opendiscussions".format(
                    data["username"], data["password"], vault_env_path
                )
            )
        )
    )

heroku_app_id = heroku_config.require("app_id")
opendiscussions_heroku_configassociation = heroku.app.ConfigAssociation(
    f"ol-opendiscussions-heroku-configassociation-{stack_info.env_suffix}",
    app_id=heroku_app_id,
    vars=heroku_vars,
    sensitive_vars=sensitive_heroku_vars,
)


# Need to reconcile with other stack refs first.
export(
    "mit_open",
    {
        "iam_policy": mit_open_iam_policy.arn,
        "vault_iam_role": Output.all(
            mit_open_vault_iam_role.backend, mit_open_vault_iam_role.name
        ).apply(lambda role: f"{role[0]}/roles/{role[1]}"),
    },
)
