# ruff: noqa: TD003, ERA001, FIX002, E501

import base64
import json
import textwrap
from pathlib import Path
from string import Template

import pulumi_fastly as fastly
import pulumi_github as github
import pulumi_vault as vault
import pulumiverse_heroku as heroku
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    DEFAULT_POSTGRES_PORT,
    FIVE_MINUTES,
    ONE_MEGABYTE_BYTE,
)
from bridge.secrets.sops import read_yaml_secrets
from pulumi import Config, InvokeOptions, ResourceOptions, StackReference, export
from pulumi.output import Output
from pulumi_aws import ec2, iam, route53, s3

from ol_infrastructure.components.aws.database import OLAmazonDB, OLPostgresDBConfig
from ol_infrastructure.components.services.vault import (
    OLVaultDatabaseBackend,
    OLVaultPostgresDatabaseConfig,
)
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
from ol_infrastructure.lib.heroku import setup_heroku_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.stack_defaults import defaults
from ol_infrastructure.lib.vault import postgres_role_statements, setup_vault_provider

setup_vault_provider(skip_child_token=True)
setup_heroku_provider()
fastly_provider = get_fastly_provider()
github_provider = github.Provider(
    "github-provider",
    owner=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["owner"],
    token=read_yaml_secrets(Path("pulumi/github_provider.yaml"))["token"],
)

mitopen_config = Config("mitopen")
heroku_config = Config("heroku")
heroku_app_config = Config("heroku_app")

stack_info = parse_stack()
network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
apps_vpc = network_stack.require_output("applications_vpc")
vector_log_proxy_stack = StackReference(
    f"infrastructure.vector_log_proxy.operations.{stack_info.name}"
)
monitoring_stack = StackReference("infrastructure.monitoring")
dns_stack = StackReference("infrastructure.aws.dns")
mitodl_zone_id = dns_stack.require_output("odl_zone_id")

aws_config = AWSBase(
    tags={
        "OU": "mit-open",
        "Environment": stack_info.env_suffix,
        "Application": "mitopen",
    }
)
app_env_suffix = {"ci": "ci", "qa": "rc", "production": "production"}[
    stack_info.env_suffix
]

app_storage_bucket_name = f"ol-mitopen-app-storage-{app_env_suffix}"
application_storage_bucket = s3.BucketV2(
    f"ol_mitopen_app_storage_bucket_{stack_info.env_suffix}",
    bucket=app_storage_bucket_name,
    tags=aws_config.tags,
)

s3.BucketVersioningV2(
    "ol-mitopen-bucket-versioning",
    bucket=application_storage_bucket.id,
    versioning_configuration=s3.BucketVersioningV2VersioningConfigurationArgs(
        status="Enabled"
    ),
)
app_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-mitopen-bucket-ownership-controls",
    bucket=application_storage_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
app_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-mitopen-bucket-public-access",
    bucket=application_storage_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)

s3.BucketPolicy(
    "ol-mitopen-bucket-policy",
    bucket=application_storage_bucket.id,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::{app_storage_bucket_name}/*"],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[app_bucket_public_access, app_bucket_ownership_controls]
    ),
)

course_data_bucket_name = f"ol-mitopen-course-data-{app_env_suffix}"
course_data_bucket = s3.Bucket(
    f"ol_mitopen_course_data_bucket_{stack_info.env_suffix}",
    bucket=course_data_bucket_name,
    versioning=s3.BucketVersioningArgs(enabled=True),
    cors_rules=[
        s3.BucketCorsRuleArgs(
            allowed_methods=["GET"],
            allowed_headers=["*"],
            allowed_origins=["*"],
            max_age_seconds=FIVE_MINUTES,
        )
    ],
    tags=aws_config.tags,
)

parliament_config = {
    "PERMISSIONS_MANAGEMENT_ACTIONS": {
        "ignore_locations": [{"actions": ["s3:putobjectacl"]}]
    },
    "RESOURCE_EFFECTIVELY_STAR": {},
}

gh_workflow_s3_bucket_permissions = [
    {
        "Action": [
            "s3:ListBucket*",
        ],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::{app_storage_bucket_name}",
        ],
    },
    {
        "Action": [
            "s3:GetObject*",
            "s3:PutObject",
            "s3:PutObjectAcl",
            "s3:DeleteObject",
        ],
        "Effect": "Allow",
        "Resource": [
            f"arn:aws:s3:::{app_storage_bucket_name}/frontend/*",
        ],
    },
]

gh_workflow_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": gh_workflow_s3_bucket_permissions,
}

gh_workflow_iam_policy = iam.Policy(
    f"ol_mitopen_gh_workflow_iam_permissions_{stack_info.env_suffix}",
    name=f"ol-mitopen-gh-workflow-permissions-{stack_info.env_suffix}",
    path=f"/ol-applications/mitopen/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        gh_workflow_policy_document, stringify=True, parliament_config=parliament_config
    ),
)

# Just create a static user for now. Some day refactor to use
# https://github.com/hashicorp/vault-action
gh_workflow_user = iam.User(
    f"ol_mitopen_gh_workflow_user_{stack_info.env_suffix}",
    name=f"mitopen-gh-workflow-{stack_info.env_suffix}",
    tags=aws_config.tags,
)
iam.PolicyAttachment(
    f"ol_mitopen_gh_workflow_user_{stack_info.env_suffix}",
    policy_arn=gh_workflow_iam_policy.arn,
    users=[gh_workflow_user.name],
)
gh_workflow_accesskey = iam.AccessKey(
    f"ol_mitopen_gh_workflow_accesskey-{stack_info.env_suffix}",
    user=gh_workflow_user.name,
    status="Active",
)


# TODO @Ardiea: 07312023 Requires review of bucket names
application_s3_bucket_permissions = [
    {
        "Action": [
            "s3:GetObject*",
            "s3:ListBucket*",
            "s3:PutObject",
            "s3:PutObjectAcl",
            "s3:DeleteObject",
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
            "arn:aws:s3:::ol-data-lake-landing-zone-production",
            "arn:aws:s3:::ol-data-lake-landing-zone-production/open-learning-library/*",
            "arn:aws:s3:::ol-olx-course-exports",
            "arn:aws:s3:::ol-olx-course-exports/*",
            "arn:aws:s3:::ocw-content-storage",
            "arn:aws:s3:::ocw-content-storage/*",
            f"arn:aws:s3:::ol-ocw-studio-app-{app_env_suffix}",
        ],
    },
]

application_policy_document = {
    "Version": IAM_POLICY_VERSION,
    "Statement": application_s3_bucket_permissions,
}

mitopen_iam_policy = iam.Policy(
    f"ol_mitopen_iam_permissions_{stack_info.env_suffix}",
    name=f"ol-mitopen-application-permissions-{stack_info.env_suffix}",
    path=f"/ol-applications/mitopen/{stack_info.env_suffix}/",
    policy=lint_iam_policy(
        application_policy_document, stringify=True, parliament_config=parliament_config
    ),
)


mitopen_vault_iam_role = vault.aws.SecretBackendRole(
    f"ol-mitopen-iam-permissions-vault-policy-{stack_info.env_suffix}",
    name="ol-mitopen-application",
    backend="aws-mitx",
    credential_type="iam_user",
    policy_arns=[mitopen_iam_policy.arn],
)

mitopen_vault_mount = vault.Mount(
    f"ol-mitopen-configuration-secrets-mount-{stack_info.env_suffix}",
    path="secret-mitopen",
    type="kv-v2",
    options={"version": 2},
    description="Storage of configuration secrets used by MIT-Open",
    opts=ResourceOptions(delete_before_replace=True),
)

mitopen_vault_secrets = read_yaml_secrets(
    Path(f"mitopen/secrets.{stack_info.env_suffix}.yaml"),
)

vault.generic.Secret(
    f"ol-mitopen-configuration-secrets-{stack_info.env_suffix}",
    path=mitopen_vault_mount.path.apply("{}/secrets".format),
    data_json=json.dumps(mitopen_vault_secrets),
)

mitopen_db_security_group = ec2.SecurityGroup(
    f"ol-mitopen-db-access-{stack_info.env_suffix}",
    description=f"Access control for the MIT Open application DB in {stack_info.name}",
    ingress=[
        ec2.SecurityGroupIngressArgs(
            protocol="tcp",
            from_port=DEFAULT_POSTGRES_PORT,
            to_port=DEFAULT_POSTGRES_PORT,
            cidr_blocks=["0.0.0.0/0"],
            ipv6_cidr_blocks=["::/0"],
            description="Allow access over the public internet from Heroku.",
        )
    ],
    egress=[
        ec2.SecurityGroupEgressArgs(
            from_port=0,
            to_port=0,
            protocol="-1",
            cidr_blocks=["0.0.0.0/32"],
            ipv6_cidr_blocks=["::/0"],
        )
    ],
    tags=aws_config.tags,
    vpc_id=apps_vpc["id"],
)

rds_password = mitopen_config.require("db_password")
rds_defaults = defaults(stack_info)["rds"]
rds_defaults["instance_size"] = (
    mitopen_config.get("db_instance_size") or rds_defaults["instance_size"]
)
mitopen_db_config = OLPostgresDBConfig(
    instance_name=f"ol-mitopen-db-{stack_info.env_suffix}",
    password=rds_password,
    subnet_group_name=apps_vpc["rds_subnet"],
    security_groups=[mitopen_db_security_group],
    engine_major_version="15",
    tags=aws_config.tags,
    db_name="mitopen",
    public_access=True,
    **rds_defaults,
)
mitopen_db_config.parameter_overrides.append(
    {"name": "password_encryption", "value": "md5"}
)

mitopen_db = OLAmazonDB(mitopen_db_config)

mitopen_role_statements = postgres_role_statements.copy()
mitopen_role_statements.pop("app")
mitopen_role_statements["app"] = {
    "create": Template(
        """CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
            VALID UNTIL '{{expiration}}' IN ROLE "mitopen" INHERIT;
          GRANT CREATE ON SCHEMA public TO mitopen WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO "mitopen"
            WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO "mitopen"
            WITH GRANT OPTION;
          SET ROLE "mitopen";
          ALTER DEFAULT PRIVILEGES FOR ROLE "mitopen" IN SCHEMA public
            GRANT ALL PRIVILEGES ON TABLES TO "mitopen" WITH GRANT OPTION;

          ALTER DEFAULT PRIVILEGES FOR ROLE "mitopen" IN SCHEMA public
            GRANT ALL PRIVILEGES ON SEQUENCES TO "mitopen" WITH GRANT OPTION;

          CREATE SCHEMA IF NOT EXISTS external;

          GRANT CREATE ON SCHEMA external TO "mitopen" WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA external TO "mitopen";
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA external TO "mitopen"
            WITH GRANT OPTION;
          RESET ROLE;
          ALTER ROLE "{{name}}" SET ROLE "mitopen";"""
    ),
    "revoke": Template(
        """REVOKE "mitopen" FROM "{{name}}";
          GRANT "{{name}}" TO mitopen WITH ADMIN OPTION;
          SET ROLE mitopen;
          REASSIGN OWNED BY "{{name}}" TO "mitopen";
          RESET ROLE;
          DROP OWNED BY "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA external FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA external FROM "{{name}}";
          REVOKE USAGE ON SCHEMA public FROM "{{name}}";
          REVOKE USAGE ON SCHEMA external FROM "{{name}}";
          DROP USER "{{name}}";"""
    ),
}
mitopen_role_statements["reverse-etl"] = {
    "create": Template(
        """CREATE USER "{{name}}" WITH PASSWORD '{{password}}'
            VALID UNTIL '{{expiration}}';
          GRANT CREATE ON SCHEMA external TO "{{name}}" WITH GRANT OPTION;
          GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA external TO "{{name}}";
          GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA external TO "{{name}}"
            WITH GRANT OPTION;
        """
    ),
    "revoke": Template(
        """GRANT "{{name}}" TO mitopen WITH ADMIN OPTION;
          SET ROLE mitopen;
          REASSIGN OWNED BY "{{name}}" TO "mitopen";
          RESET ROLE;
          DROP OWNED BY "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA external FROM "{{name}}";
          REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA external FROM "{{name}}";
          REVOKE USAGE ON SCHEMA external FROM "{{name}}";
          DROP USER "{{name}}";"""
    ),
}


mitopen_vault_backend_config = OLVaultPostgresDatabaseConfig(
    db_name=mitopen_db_config.db_name,
    mount_point=f"{mitopen_db_config.engine}-mitopen",
    db_admin_username=mitopen_db_config.username,
    db_admin_password=rds_password,
    db_host=mitopen_db.db_instance.address,
    role_statements=mitopen_role_statements,
)
mitopen_vault_backend = OLVaultDatabaseBackend(mitopen_vault_backend_config)

vector_log_proxy_secrets = read_yaml_secrets(
    Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
)
fastly_proxy_credentials = vector_log_proxy_secrets["fastly"]
encoded_fastly_proxy_credentials = base64.b64encode(
    f"{fastly_proxy_credentials['username']}:{fastly_proxy_credentials['password']}".encode()
).decode("utf8")
vector_log_proxy_fqdn = vector_log_proxy_stack.require_output("vector_log_proxy")[
    "fqdn"
]

fastly_access_logging_bucket = monitoring_stack.require_output(
    "fastly_access_logging_bucket"
)
fastly_access_logging_iam_role = monitoring_stack.require_output(
    "fastly_access_logging_iam_role"
)
open_api_paths = list(mitopen_config.require_object("api_paths"))
api_regex = "^/({})".format("|".join(open_api_paths))
# Ref: https://github.com/mitodl/mit-open/blob/main/config/nginx.conf
mitopen_fastly_service = fastly.ServiceVcl(
    f"fastly-{stack_info.env_prefix}-{stack_info.env_suffix}",
    name=f"MIT Open {stack_info.env_suffix}",
    comment="Managed by Pulumi",
    backends=[
        fastly.ServiceVclBackendArgs(
            address=application_storage_bucket.bucket_domain_name,
            name="MITOpen Frontend",
            override_host=application_storage_bucket.bucket_domain_name,
            port=DEFAULT_HTTPS_PORT,
            request_condition="frontend path",
            ssl_cert_hostname=application_storage_bucket.bucket_domain_name,
            ssl_sni_hostname=application_storage_bucket.bucket_domain_name,
            use_ssl=True,
        ),
        fastly.ServiceVclBackendArgs(
            address=mitopen_config.require("heroku_domain"),
            name="MITOpen API",
            override_host=mitopen_config.require("domain"),
            port=DEFAULT_HTTPS_PORT,
            request_condition="api path",
            ssl_cert_hostname=mitopen_config.require("domain"),
            ssl_sni_hostname=mitopen_config.require("domain"),
            use_ssl=True,
        ),
    ],
    cache_settings=[],
    conditions=[
        fastly.ServiceVclConditionArgs(
            name="api path",
            statement=f'req.url ~ "{api_regex}/"',
            type="REQUEST",
        ),
        fastly.ServiceVclConditionArgs(
            name="frontend path",
            statement=f'req.url !~ "{api_regex}/"',
            type="REQUEST",
        ),
    ],
    dictionaries=[],
    domains=[
        fastly.ServiceVclDomainArgs(
            comment=f"{stack_info.env_prefix} {stack_info.env_suffix} Application",
            name=mitopen_config.require("domain"),
        ),
    ],
    headers=[
        fastly.ServiceVclHeaderArgs(
            action="set",
            destination="http.Strict-Transport-Security",
            name="Generated by force TLS and enable HSTS",
            source='"max-age=300"',
            type="response",
        ),
    ],
    request_settings=[],
    snippets=[
        fastly.ServiceVclSnippetArgs(
            name="Rewrite requests to root s3",
            content=textwrap.dedent(
                rf"""
                if (req.url.path !~ "{api_regex}") {{
                   set req.http.orig-req-url = req.url;
                   set req.url = "/frontend" + req.url;
                }}
                if (req.http.retry-for-dir) {{
                  set req.url = req.url + "/";
                }}

                # Fetch the index page if the request is for a directory
                if (req.url ~ "\/$" && req.url !~ "{api_regex}") {{
                  set req.url = req.url + "index.html";
                }}
                """
            ),
            type="recv",
        )
    ],
    logging_https=[
        fastly.ServiceVclLoggingHttpArgs(
            url=Output.all(fqdn=vector_log_proxy_fqdn).apply(
                lambda fqdn: "https://{fqdn}".format(**fqdn)
            ),
            name=f"fastly-{stack_info.env_prefix}-{stack_info.env_suffix}-https-logging-args",
            content_type="application/json",
            format=build_fastly_log_format_string(additional_static_fields={}),
            format_version=2,
            header_name="Authorization",
            header_value=f"Basic {encoded_fastly_proxy_credentials}",
            json_format="0",
            method="POST",
            request_max_bytes=ONE_MEGABYTE_BYTE,
        )
    ],
    opts=fastly_provider,
)

five_minutes = 60 * 5
route53.Record(
    "ol-mitopen-frontend-dns-record",
    name=mitopen_config.require("domain"),
    type="CNAME",
    ttl=five_minutes,
    records=["d.sni.global.fastly.net"],
    zone_id=mitodl_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)


# ci, rc, or production
env_name = stack_info.name.lower() if stack_info.name != "QA" else "rc"

# Values that are generally unchanging across environments
heroku_vars = {
    "ALLOWED_HOSTS": '["*"]',
    "AWS_STORAGE_BUCKET_NAME": f"ol-mitopen-app-storage-{env_name}",
    "CORS_ALLOWED_ORIGIN_REGEXES": "['^.+ocw-next.netlify.app$']",
    "CSAIL_BASE_URL": "https://cap.csail.mit.edu/",
    "DUPLICATE_COURSES_URL": "https://raw.githubusercontent.com/mitodl/open-resource-blacklists/master/duplicate_courses.yml",
    "EDX_API_ACCESS_TOKEN_URL": "https://api.edx.org/oauth2/v1/access_token",
    "EDX_API_URL": "https://api.edx.org/catalog/v1/catalogs/10/courses",
    "MICROMASTERS_CATALOG_API_URL": "https://micromasters.mit.edu/api/v0/catalog/",
    "MICROMASTERS_CMS_API_URL": "https://micromasters.mit.edu/api/v0/wagtail/",
    "MITOPEN_ADMIN_EMAIL": "cuddle-bunnies@mit.edu",
    "MITOPEN_DB_CONN_MAX_AGE": 0,
    "MITOPEN_DB_DISABLE_SSL": "True",
    "MITOPEN_DEFAULT_SITE_KEY": "micromasters",
    "MITOPEN_EMAIL_PORT": 587,
    "MITOPEN_EMAIL_TLS": "True",
    "MITOPEN_ENVIRONMENT": env_name,
    "MITOPEN_FROM_EMAIL": "MITOpen <mitopen-support@mit.edu>",
    "MITOPEN_FRONTPAGE_DIGEST_MAX_POSTS": 10,
    "MITOPEN_USE_S3": "True",
    "MITPE_BASE_URL": "https://professional.mit.edu/",
    "MITX_ONLINE_BASE_URL": "https://mitxonline.mit.edu/",
    "MITX_ONLINE_COURSES_API_URL": "https://mitxonline.mit.edu/api/courses/",
    "MITX_ONLINE_LEARNING_COURSE_BUCKET_NAME": "mitx-etl-mitxonline-production",
    "MITX_ONLINE_PROGRAMS_API_URL": "https://mitxonline.mit.edu/api/programs/",
    "NEW_RELIC_LOG": "stdout",
    "NODE_MODULES_CACHE": "False",
    "OCW_BASE_URL": "https://ocw.mit.edu/",
    "OCW_CONTENT_BUCKET_NAME": "ocw-content-storage",
    "OCW_UPLOAD_IMAGE_ONLY": "True",
    "OCW_LIVE_BUCKET": "ocw-content-live-production",
    "OLL_ALT_URL": "https://openlearninglibrary.mit.edu/courses/",
    "OLL_API_ACCESS_TOKEN_URL": "https://openlearninglibrary.mit.edu/oauth2/access_token/",
    "OLL_API_URL": "https://discovery.openlearninglibrary.mit.edu/api/v1/catalogs/1/courses/",
    "OLL_BASE_URL": "https://openlearninglibrary.mit.edu/course/",
    "OLL_LEARNING_COURSE_BUCKET_NAME": "ol-data-lake-landing-zone-production",
    "OLL_LEARNING_COURSE_BUCKET_PREFIX": "open-learning-library/courses/",
    "OPENSEARCH_DEFAULT_TIMEOUT": 30,
    "OPENSEARCH_INDEXING_CHUNK_SIZE": 75,
    "PROLEARN_CATALOG_API_URL": "https://prolearn.mit.edu/graphql",
    "SEE_BASE_URL": "https://executive.mit.edu/",
    "SOCIAL_AUTH_OL_OIDC_KEY": "ol-open-client",
    "USE_X_FORWARDED_HOST": "True",
    "USE_X_FORWARDED_PORT": "True",
    "XPRO_CATALOG_API_URL": "https://xpro.mit.edu/api/programs/",
    "XPRO_COURSES_API_URL": "https://xpro.mit.edu/api/courses/",
    "XPRO_LEARNING_COURSE_BUCKET_NAME": "mitx-etl-xpro-production-mitxpro-production",
    "YOUTUBE_FETCH_TRANSCRIPT_SCHEDULE_SECONDS": 21600,
    "YOUTUBE_CONFIG_URL": "https://raw.githubusercontent.com/mitodl/open-video-data/mitopen/youtube/channels.yaml",
    "POSTHOG_ENABLED": "True",
    "POSTHOG_TIMEOUT_MS": 1000,
    "POSTHOG_API_HOST": "https://app.posthog.com",
    "POSTHOG_PROJECT_ID": 63497,
}

# Values that require interpolation or other special considerations
interpolation_vars = heroku_app_config.get_object("interpolation_vars")

cors_urls_list = interpolation_vars["cors_urls"] or []
cors_urls_json = json.dumps(cors_urls_list)
auth_allowed_redirect_hosts_list = (
    interpolation_vars["auth_allowed_redirect_hosts"] or []
)
auth_allowed_redirect_hosts_json = json.dumps(auth_allowed_redirect_hosts_list)

heroku_interpolated_vars = {
    "ACCESS_TOKEN_URL": f"https://{interpolation_vars['sso_url']}/realms/olapps/protocol/openid-connect/token",
    "AUTHORIZATION_URL": f"https://{interpolation_vars['sso_url']}/realms/olapps/protocol/openid-connect/auth",
    "CORS_ALLOWED_ORIGINS": cors_urls_json,
    "KEYCLOAK_BASE_URL": f"https://{interpolation_vars['sso_url']}/",
    "MAILGUN_FROM_EMAIL": f"MIT Open <no-reply@{interpolation_vars['mailgun_sender_domain']}",
    "MAILGUN_SENDER_DOMAIN": interpolation_vars["mailgun_sender_domain"],
    "MAILGUN_URL": f"https://api.mailgun.net/v3/{interpolation_vars['mailgun_sender_domain']}",
    "MITOPEN_CORS_ORIGIN_WHITELIST": cors_urls_json,
    "OIDC_ENDPOINT": f"https://{interpolation_vars['sso_url']}/realms/olapps",
    "SOCIAL_AUTH_ALLOWED_REDIRECT_HOSTS": auth_allowed_redirect_hosts_json,
    "SOCIAL_AUTH_OL_OIDC_OIDC_ENDPOINT": f"https://{interpolation_vars['sso_url']}/realms/olapps",
    "USERINFO_URL": f"https://{interpolation_vars['sso_url']}/realms/olapps/protocol/openid-connect/userinfo",
}

# Combine two var sources above with values explictly defined in pulumi configuration
heroku_vars.update(**heroku_interpolated_vars)
heroku_vars.update(**heroku_app_config.get_object("vars"))

# Making these `get_secret_*()` calls children of the seemigly un-related vault mount `secret-mitopen/` tricks
# them into inheriting the correct vault provider rather than attempting to create their own (which won't work and / or
# will duplicate the existing vault provider)

# TODO @Ardiea: 01112024 We should be able to use vault.aws.get_access_credentials_output()
# for this but it doesn't seem to work
# auth_aws_mitx_creds_ol_mitopen_application = vault.aws.get_access_credentials_output(backend=mitopen_vault_iam_role.backend, role=mitopen_vault_iam_role.name, opts=InvokeOptions(parent=mitopen_vault_mount))  # . TD003
auth_aws_mitx_creds_ol_mitopen_application = vault.generic.get_secret_output(
    path="aws-mitx/creds/ol-mitopen-application",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=mitopen_vault_mount),
)
auth_postgres_mitopen_creds_app = vault.generic.get_secret_output(
    path="postgres-mitopen/creds/app",
    with_lease_start_time=False,
    opts=InvokeOptions(parent=mitopen_vault_mount),
)

secret_operations_global_embedly = vault.generic.get_secret_output(
    path="secret-operations/global/embedly",
    opts=InvokeOptions(parent=mitopen_vault_mount),
)
secret_operations_global_odlbot_github_access_token = vault.generic.get_secret_output(
    path="secret-operations/global/odlbot-github-access-token",
    opts=InvokeOptions(parent=mitopen_vault_mount),
)
secret_operations_global_mailgun_api_key = vault.generic.get_secret_output(
    path="secret-operations/global/mailgun-api-key",
    opts=InvokeOptions(parent=mitopen_vault_mount),
)
secret_operations_global_mit_smtp = vault.generic.get_secret_output(
    path="secret-operations/global/mit-smtp",
    opts=InvokeOptions(parent=mitopen_vault_mount),
)
secret_operations_global_update_search_data_webhook_key = (
    vault.generic.get_secret_output(
        path="secret-operations/global/update-search-data-webhook-key",
        opts=InvokeOptions(parent=mitopen_vault_mount),
    )
)
secret_operations_sso_open = vault.generic.get_secret_output(
    path="secret-operations/sso/open", opts=InvokeOptions(parent=mitopen_vault_mount)
)
secret_operations_tika_access_token = vault.generic.get_secret_output(
    path="secret-operations/tika/access-token",
    opts=InvokeOptions(parent=mitopen_vault_mount),
)

# Gets masked in any console outputs
sensitive_heroku_vars = {
    # Vars available locally from SOPS
    "CKEDITOR_ENVIRONMENT_ID": mitopen_vault_secrets["ckeditor"]["environment_id"],
    "CKEDITOR_SECRET_KEY": mitopen_vault_secrets["ckeditor"]["secret_key"],
    "CKEDITOR_UPLOAD_URL": mitopen_vault_secrets["ckeditor"]["upload_url"],
    "EDX_API_CLIENT_ID": mitopen_vault_secrets["edx-api-client"]["id"],
    "EDX_API_CLIENT_SECRET": mitopen_vault_secrets["edx-api-client"]["secret"],
    "MITOPEN_JWT_SECRET": mitopen_vault_secrets["jwt_secret"],
    "OLL_API_CLIENT_ID": mitopen_vault_secrets["open-learning-library-client"][
        "client-id"
    ],
    "OLL_API_CLIENT_SECRET": mitopen_vault_secrets["open-learning-library-client"][
        "client-secret"
    ],
    "OPENSEARCH_HTTP_AUTH": mitopen_vault_secrets["opensearch"]["http_auth"],
    "SECRET_KEY": mitopen_vault_secrets["django-secret-key"],
    "SENTRY_DSN": mitopen_vault_secrets["sentry-dsn"],
    "STATUS_TOKEN": mitopen_vault_secrets["django-status-token"],
    "YOUTUBE_DEVELOPER_KEY": mitopen_vault_secrets["youtube-developer-key"],
    "POSTHOG_PROJECT_API_KEY": mitopen_vault_secrets["posthog"]["project_api_key"],
    "POSTHOG_PERSONAL_API_KEY": mitopen_vault_secrets["posthog"]["personal_api_key"],
    # Vars that require more
    "AWS_ACCESS_KEY_ID": auth_aws_mitx_creds_ol_mitopen_application.data.apply(
        lambda data: "{}".format(data["access_key"])
    ),  # TODO @Ardiea: This changes every run / preview and creates a mess in IAM.
    "AWS_SECRET_ACCESS_KEY": auth_aws_mitx_creds_ol_mitopen_application.data.apply(
        lambda data: "{}".format(data["secret_key"])
    ),
    "DATABASE_URL": auth_postgres_mitopen_creds_app.data.apply(
        lambda data: "postgres://{}:{}@ol-mitopen-db-{}.cbnm7ajau6mi.us-east-1.rds.amazonaws.com:5432/mitopen".format(
            data["username"], data["password"], stack_info.name.lower()
        )
    ),  # TODO @Ardiea: This changes every run / preview and creates a mess in the DB.
    "EMBEDLY_KEY": secret_operations_global_embedly.data.apply(
        lambda data: "{}".format(data["key"])
    ),
    "GITHUB_ACCESS_TOKEN": secret_operations_global_odlbot_github_access_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "MAILGUN_KEY": secret_operations_global_mailgun_api_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "MITOPEN_EMAIL_HOST": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_host"])
    ),
    "MITOPEN_EMAIL_PASSWORD": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_password"])
    ),
    "MITOPEN_EMAIL_USER": secret_operations_global_mit_smtp.data.apply(
        lambda data: "{}".format(data["relay_username"])
    ),
    "OCW_NEXT_SEARCH_WEBHOOK_KEY": secret_operations_global_update_search_data_webhook_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "OCW_WEBHOOK_KEY": secret_operations_global_update_search_data_webhook_key.data.apply(
        lambda data: "{}".format(data["value"])
    ),
    "SOCIAL_AUTH_OL_OIDC_SECRET": secret_operations_sso_open.data.apply(
        lambda data: "{}".format(data["client_secret"])
    ),
    "TIKA_ACCESS_TOKEN": secret_operations_tika_access_token.data.apply(
        lambda data: "{}".format(data["value"])
    ),
}

heroku_app_id = heroku_config.require("app_id")
mitopen_heroku_configassociation = heroku.app.ConfigAssociation(
    f"ol-mitopen-heroku-configassociation-{stack_info.env_suffix}",
    app_id=heroku_app_id,
    sensitive_vars=sensitive_heroku_vars,
    vars=heroku_vars,
)

env_var_suffix = "RC" if stack_info.env_suffix == "qa" else "PROD"

gh_repo = github.get_repository(
    full_name="mitodl/mit-open", opts=InvokeOptions(provider=github_provider)
)
gh_workflow_accesskey_id_env_secret = github.ActionsSecret(
    f"ol_mitopen_gh_workflow_accesskey_id_env_secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"AWS_ACCESS_KEY_ID_{env_var_suffix}",
    plaintext_value=gh_workflow_accesskey.id,
    opts=ResourceOptions(provider=github_provider),
)
gh_workflow_secretaccesskey_env_secret = github.ActionsSecret(
    f"ol_mitopen_gh_workflow_secretaccesskey_env_secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"AWS_SECRET_ACCESS_KEY_{env_var_suffix}",
    plaintext_value=gh_workflow_accesskey.secret,
    opts=ResourceOptions(provider=github_provider),
)
gh_workflow_embedlykey_env_secret = github.ActionsSecret(
    f"ol_mitopen_gh_workflow_embedlykey_env_secret-{stack_info.env_suffix}",
    repository=gh_repo.name,
    secret_name=f"EMBEDLY_KEY_{env_var_suffix}",
    plaintext_value=secret_operations_global_embedly.data.apply(
        lambda data: "{}".format(data["key"])
    ),
    opts=ResourceOptions(provider=github_provider),
)

export(
    "mitopen",
    {
        "iam_policy": mitopen_iam_policy.arn,
        "vault_iam_role": Output.all(
            mitopen_vault_iam_role.backend, mitopen_vault_iam_role.name
        ).apply(lambda role: f"{role[0]}/roles/{role[1]}"),
    },
)
