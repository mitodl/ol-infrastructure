import base64
import json
from pathlib import Path

import httpx
import pulumi_fastly as fastly
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_2, FASTLY_CNAME_TLS_1_3
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    FIVE_MINUTES,
    HTTP_STATUS_NOT_FOUND,
    HTTP_STATUS_OK,
    ONE_MEGABYTE_BYTE,
    SECONDS_IN_ONE_DAY,
)
from bridge.secrets.sops import read_yaml_secrets
from ol_infrastructure.lib.aws.iam_helper import IAM_POLICY_VERSION, lint_iam_policy
from ol_infrastructure.lib.fastly import (
    build_fastly_log_format_string,
    get_fastly_provider,
)
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

ocw_site_config = Config("ocw_site")
stack_info = parse_stack()
aws_config = AWSBase(
    tags={
        "OU": "open-courseware",
        "Environment": f"applications_{stack_info.env_suffix}",
    }
)
fastly_provider = get_fastly_provider()

dns_stack = StackReference("infrastructure.aws.dns")
ocw_zone = dns_stack.require_output("ocw")

vector_log_proxy_stack = StackReference(
    f"infrastructure.vector_log_proxy.operations.{stack_info.name}"
)
vector_log_proxy_fqdn = vector_log_proxy_stack.require_output("vector_log_proxy")[
    "fqdn"
]

vector_log_proxy_secrets = read_yaml_secrets(
    Path(f"vector/vector_log_proxy.{stack_info.env_suffix}.yaml")
)
fastly_proxy_credentials = vector_log_proxy_secrets["fastly"]
encoded_fastly_proxy_credentials = base64.b64encode(
    f"{fastly_proxy_credentials['username']}:{fastly_proxy_credentials['password']}".encode()
).decode("utf8")

monitoring_stack = StackReference("infrastructure.monitoring")
fastly_access_logging_bucket = monitoring_stack.require_output(
    "fastly_access_logging_bucket"
)
fastly_access_logging_iam_role = monitoring_stack.require_output(
    "fastly_access_logging_iam_role"
)

# Create S3 buckets
# There are two buckets for each environment (QA, Production):
# One for the that environment's draft site (where authors test content
# changes), and one for the environment's live site.
# See http://docs.odl.mit.edu/ocw-next/s3-buckets

draft_bucket_name = f"ocw-content-draft-{stack_info.env_suffix}"
draft_bucket_arn = f"arn:aws:s3:::{draft_bucket_name}"
live_bucket_name = f"ocw-content-live-{stack_info.env_suffix}"
live_bucket_arn = f"arn:aws:s3:::{live_bucket_name}"
test_bucket_name = f"ocw-content-test-{stack_info.env_suffix}"
test_bucket_arn = f"arn:aws:s3:::{test_bucket_name}"

draft_backup_bucket_name = f"ocw-content-backup-draft-{stack_info.env_suffix}"
draft_backup_bucket_arn = f"arn:aws:s3:::{draft_backup_bucket_name}"
live_backup_bucket_name = f"ocw-content-backup-live-{stack_info.env_suffix}"
live_backup_bucket_arn = f"arn:aws:s3:::{live_backup_bucket_name}"

draft_offline_bucket_name = f"ocw-content-offline-draft-{stack_info.env_suffix}"
draft_offline_bucket_arn = f"arn:aws:s3:::{draft_offline_bucket_name}"
live_offline_bucket_name = f"ocw-content-offline-live-{stack_info.env_suffix}"
live_offline_bucket_arn = f"arn:aws:s3:::{live_offline_bucket_name}"
test_offline_bucket_name = f"ocw-content-offline-test-{stack_info.env_suffix}"
test_offline_bucket_arn = f"arn:aws:s3:::{test_offline_bucket_name}"

# Draft bucket
draft_bucket = s3.Bucket(
    draft_bucket_name,
    bucket=draft_bucket_name,
    tags=aws_config.tags,
)
draft_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-draft-bucket-ownership-controls",
    bucket=draft_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-draft-bucket-versioning",
    bucket=draft_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)

draft_bucket_cors = s3.BucketCorsConfiguration(
    "ol-draft-bucket-cors",
    bucket=draft_bucket_name,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
)
draft_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-draft-bucket-public-access",
    bucket=draft_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-draft-bucket-policy",
    bucket=draft_bucket.id,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{draft_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            draft_bucket_public_access,
            draft_bucket_ownership_controls,
        ]
    ),
)

# test bucket
test_bucket = s3.Bucket(
    test_bucket_name,
    bucket=test_bucket_name,
    tags=aws_config.tags,
)
test_bucket_cors = s3.BucketCorsConfiguration(
    "ol-test-bucket-cors",
    bucket=test_bucket_name,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
)
test_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-test-bucket-ownership-controls",
    bucket=test_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-test-bucket-versioning",
    bucket=test_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
test_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-test-bucket-public-access",
    bucket=test_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-test-bucket-policy",
    bucket=test_bucket.id,
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{test_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            test_bucket_public_access,
            test_bucket_ownership_controls,
        ]
    ),
)

# live bucket
live_bucket = s3.Bucket(
    live_bucket_name,
    bucket=live_bucket_name,
    tags=aws_config.tags,
)
live_bucket_cors = s3.BucketCorsConfiguration(
    "ol-live-bucket-cors",
    bucket=live_bucket_name,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
)
live_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-live-bucket-ownership-controls",
    bucket=live_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-live-bucket-versioning",
    bucket=live_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
live_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-live-bucket-public-access",
    bucket=live_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-live-bucket-policy",
    bucket=live_bucket.id,
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{live_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            live_bucket_public_access,
            live_bucket_ownership_controls,
        ]
    ),
)

# draft_backup bucket
draft_backup_bucket = s3.Bucket(
    draft_backup_bucket_name,
    bucket=draft_backup_bucket_name,
    tags=aws_config.tags,
)
draft_backup_bucket_cors = s3.BucketCorsConfiguration(
    "ol-draft-backup-bucket-cors",
    bucket=draft_backup_bucket_name,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
)
draft_backup_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-draft-backup-bucket-ownership-controls",
    bucket=draft_backup_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-draft-backup-bucket-versioning",
    bucket=draft_backup_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
draft_backup_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-draft-backup-bucket-public-access",
    bucket=draft_backup_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-draft-backup-bucket-policy",
    bucket=draft_backup_bucket.id,
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{draft_backup_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            draft_backup_bucket_public_access,
            draft_backup_bucket_ownership_controls,
        ]
    ),
)

# live_backup bucket
live_backup_bucket = s3.Bucket(
    live_backup_bucket_name,
    bucket=live_backup_bucket_name,
    tags=aws_config.tags,
)
live_backup_bucket_cors = s3.BucketCorsConfiguration(
    "ol-live-backup-bucket-cors",
    bucket=live_backup_bucket_name,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
)
live_backup_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-live-backup-bucket-ownership-controls",
    bucket=live_backup_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-live-backup-bucket-versioning",
    bucket=live_backup_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
live_backup_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-live-backup-bucket-public-access",
    bucket=live_backup_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-live-backup-bucket-policy",
    bucket=live_backup_bucket.id,
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{live_backup_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            live_backup_bucket_public_access,
            live_backup_bucket_ownership_controls,
        ]
    ),
)

# draft_backup bucket
draft_offline_bucket = s3.Bucket(
    draft_offline_bucket_name,
    bucket=draft_offline_bucket_name,
    tags=aws_config.tags,
)
draft_offline_bucket_cors = s3.BucketCorsConfiguration(
    "ol-draft-offline-bucket-cors",
    bucket=draft_offline_bucket_name,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
)
draft_offline_bucket_website = s3.BucketWebsiteConfiguration(
    "draft-offline-website",
    bucket=draft_offline_bucket_name,
    index_document=s3.BucketWebsiteConfigurationIndexDocumentArgs(
        suffix="index.html",
    ),
    error_document=s3.BucketWebsiteConfigurationErrorDocumentArgs(
        key="error.html",
    ),
)

draft_offline_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-offline-backup-bucket-ownership-controls",
    bucket=draft_offline_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-offline-backup-bucket-versioning",
    bucket=draft_offline_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
draft_offline_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-offline-backup-bucket-public-access",
    bucket=draft_offline_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-offline-backup-bucket-policy",
    bucket=draft_offline_bucket.id,
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{draft_offline_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            draft_offline_bucket_public_access,
            draft_offline_bucket_ownership_controls,
        ]
    ),
)

# live_backup bucket
live_offline_bucket = s3.Bucket(
    live_offline_bucket_name,
    bucket=live_offline_bucket_name,
    tags=aws_config.tags,
)
live_offline_bucket_website = s3.BucketWebsiteConfiguration(
    "live-offline-website",
    bucket=live_offline_bucket_name,
    index_document=s3.BucketWebsiteConfigurationIndexDocumentArgs(
        suffix="index.html",
    ),
    error_document=s3.BucketWebsiteConfigurationErrorDocumentArgs(
        key="error.html",
    ),
)

live_offline_bucket_cors = s3.BucketCorsConfiguration(
    "ol-live-offline-bucket-cors",
    bucket=live_offline_bucket_name,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
)
live_offline_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-live-offline-bucket-ownership-controls",
    bucket=live_offline_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-live-offline-bucket-versioning",
    bucket=live_offline_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
live_offline_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-live-offline-bucket-public-access",
    bucket=live_offline_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-live-offline-bucket-policy",
    bucket=live_offline_bucket.id,
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{live_offline_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            live_offline_bucket_public_access,
            live_offline_bucket_ownership_controls,
        ]
    ),
)

# test_backup bucket
test_offline_bucket = s3.Bucket(
    test_offline_bucket_name,
    bucket=test_offline_bucket_name,
    tags=aws_config.tags,
)
test_offline_bucket_cors = s3.BucketCorsConfiguration(
    "ol-test-offline-bucket-cors",
    bucket=test_offline_bucket_name,
    cors_rules=[
        s3.BucketCorsConfigurationCorsRuleArgs(
            allowed_methods=["GET", "HEAD"],
            allowed_origins=["*"],
        )
    ],
)
test_offline_bucket_ownership_controls = s3.BucketOwnershipControls(
    "ol-test-offline-bucket-ownership-controls",
    bucket=test_offline_bucket.id,
    rule=s3.BucketOwnershipControlsRuleArgs(
        object_ownership="BucketOwnerPreferred",
    ),
)
s3.BucketVersioning(
    "ol-test-offline-bucket-versioning",
    bucket=test_offline_bucket.id,
    versioning_configuration=s3.BucketVersioningVersioningConfigurationArgs(
        status="Enabled"
    ),
)
test_offline_bucket_public_access = s3.BucketPublicAccessBlock(
    "ol-test-offline-bucket-public-access",
    bucket=test_offline_bucket.id,
    block_public_acls=False,
    block_public_policy=False,
    ignore_public_acls=False,
)
s3.BucketPolicy(
    "ol-test-offline-bucket-policy",
    bucket=test_offline_bucket.id,
    policy=json.dumps(
        {
            "Version": IAM_POLICY_VERSION,
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [
                        f"{test_offline_bucket_arn}/*",
                    ],
                }
            ],
        }
    ),
    opts=ResourceOptions(
        depends_on=[
            test_offline_bucket_public_access,
            test_offline_bucket_ownership_controls,
        ]
    ),
)

policy_description = (
    "Access controls for the CDN to be able to read from the"
    f"{stack_info.env_suffix} website buckets"
)
s3_bucket_iam_policy = iam.Policy(
    f"ocw-site-{stack_info.env_suffix}-policy",
    description=policy_description,
    path=f"/ol-applications/ocw-site/{stack_info.env_suffix}/",
    name_prefix=f"ocw-site-content-read-only-{stack_info.env_suffix}",
    policy=lint_iam_policy(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Action": [
                        "s3:ListBucket*",
                        "s3:GetObject*",
                    ],
                    "Resource": [
                        draft_bucket_arn,
                        f"{draft_bucket_arn}/*",
                        test_bucket_arn,
                        f"{test_bucket_arn}/*",
                        live_bucket_arn,
                        f"{live_bucket_arn}/*",
                        draft_backup_bucket_arn,
                        f"{draft_backup_bucket_arn}/*",
                        draft_offline_bucket_arn,
                        f"{draft_offline_bucket_arn}/*",
                        live_bucket_arn,
                        f"{live_bucket_arn}/*",
                        live_backup_bucket_arn,
                        f"{live_backup_bucket_arn}/*",
                        live_offline_bucket_arn,
                        f"{live_offline_bucket_arn}/*",
                        test_offline_bucket_arn,
                        f"{test_offline_bucket_arn}/*",
                    ],
                }
            ],
        },
        stringify=True,
    ),
)

#################
# Fastly Config #
#################
site_domains = ocw_site_config.get_object("domains") or {"draft": [], "live": []}
# Website Storage Bucket
website_storage_bucket_fqdn = "ocw-website-storage.s3.us-east-1.amazonaws.com"
project_dir = Path(__file__).resolve().parent
snippets_dir = project_dir.joinpath("snippets")
url_themes_404 = "https://raw.githubusercontent.com/mitodl/ocw-hugo-themes/release/www/layouts/404.html"
fastly_distributions: dict[str, fastly.ServiceVcl] = {}
for purpose in ("draft", "live", "test"):
    if stack_info.env_suffix == "production" and purpose == "live":
        robots_file = "robots.production.txt"
    else:
        robots_file = "robots.txt"

    website_bucket_fqdn = (
        f"ocw-content-{purpose}-{stack_info.env_suffix}.s3.us-east-1.amazonaws.com"
    )

    servicevcl_backend = fastly.ServiceVcl(
        f"ocw-{purpose}-{stack_info.env_suffix}",
        name=f"OCW {purpose.capitalize()} {stack_info.name}",
        backends=[
            fastly.ServiceVclBackendArgs(
                address=website_bucket_fqdn,
                name="WebsiteBucket",
                override_host=website_bucket_fqdn,
                port=DEFAULT_HTTPS_PORT,
                request_condition="not course media or old akamai",
                ssl_cert_hostname=website_bucket_fqdn,
                ssl_sni_hostname=website_bucket_fqdn,
                use_ssl=True,
            ),
            fastly.ServiceVclBackendArgs(
                address=website_storage_bucket_fqdn,
                name="OCWWebsiteStorageBucket",
                override_host=website_storage_bucket_fqdn,
                port=DEFAULT_HTTPS_PORT,
                request_condition="is old Akamai file",
                ssl_cert_hostname=website_storage_bucket_fqdn,
                ssl_sni_hostname=website_storage_bucket_fqdn,
                use_ssl=True,
            ),
        ],
        conditions=[
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for robots.txt",
                priority=0,
                statement='req.url.path == "/robots.txt"',
                type="REQUEST",
            ),
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for 404 page",
                statement="beresp.status == 404",
                type="CACHE",
            ),
            fastly.ServiceVclConditionArgs(
                name="not course media or old akamai",
                statement=(
                    'req.url.path !~ "^/coursemedia" && req.url.path !~ "^/ans\\d+"'
                ),
                type="REQUEST",
            ),
            fastly.ServiceVclConditionArgs(
                name="is old Akamai file",
                statement=(
                    'req.url.path ~ "^/ans\\d+" && req.url.path !~'
                    ' "/ans7870/21f/21f.027"'
                ),
                type="REQUEST",
            ),
        ],
        default_ttl=SECONDS_IN_ONE_DAY,
        dictionaries=[
            fastly.ServiceVclDictionaryArgs(
                name="redirects",
            )
        ],
        domains=[
            fastly.ServiceVclDomainArgs(name=domain) for domain in site_domains[purpose]
        ],
        gzips=[
            fastly.ServiceVclGzipArgs(
                content_types=[
                    "text/html",
                    "application/x-javascript",
                    "text/css",
                    "application/javascript",
                    "text/javascript",
                    "application/json",
                    "application/vnd.ms-fontobject",
                    "application/x-font-opentype",
                    "application/x-font-truetype",
                    "application/x-font-ttf",
                    "application/xml",
                    "font/eot",
                    "font/opentype",
                    "font/otf",
                    "image/svg+xml",
                    "image/vnd.microsoft.icon",
                    "text/plain",
                    "text/xml",
                ],
                extensions=[
                    "css",
                    "js",
                    "html",
                    "eot",
                    "ico",
                    "otf",
                    "ttf",
                    "json",
                    "svg",
                ],
                name="Generated by default gzip policy",
            )
        ],
        product_enablement=fastly.ServiceVclProductEnablementArgs(
            brotli_compression=False,
            domain_inspector=False,
            image_optimizer=False,
            origin_inspector=False,
            websockets=False,
        ),
        headers=[
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Access-Control-Allow-Origin",
                name="CORS Allow Star",
                source='"*"',
                type="cache",
            ),
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Surrogate-Key",
                name="S3 Cache Surrogate Keys",
                priority=10,
                source="beresp.http.x-amz-meta-site-id",
                type="cache",
            ),
            fastly.ServiceVclHeaderArgs(
                action="set",
                destination="http.Strict-Transport-Security",
                name="Generated by force TLS and enable HSTS",
                source='"max-age=300"',
                type="response",
            ),
        ],
        request_settings=[
            fastly.ServiceVclRequestSettingArgs(
                force_ssl=True,
                name="Generated by force TLS and enable HSTS",
            )
        ],
        response_objects=[
            fastly.ServiceVclResponseObjectArgs(
                content=snippets_dir.joinpath(robots_file).read_text(),
                content_type="text/plain",
                name="Generated by synthetic response for robots.txt",
                request_condition="Generated by synthetic response for robots.txt",
                response="OK",
                status=HTTP_STATUS_OK,
            ),
            fastly.ServiceVclResponseObjectArgs(
                cache_condition="Generated by synthetic response for 404 page",
                content=httpx.get(url_themes_404).text,
                content_type="text/html",
                name="Generated by synthetic response for 404 page",
                response="Not Found",
                status=HTTP_STATUS_NOT_FOUND,
            ),
        ],
        snippets=[
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("ttl_setup.vcl").read_text(),
                name="TTLs setup",
                priority=110,
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("departments_table.vcl").read_text(),
                name="Departments Table",
                type="init",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("s3_bucket_proxying.vcl").read_text(),
                name="S3 Bucket Proxying",
                priority=200,
                type="miss",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("redirects.vcl").read_text(),
                name="Redirects",
                type="recv",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "legacy_ocw_pages_redirect.vcl"
                ).read_text(),
                name="Legacy OCW Pages Redirect",
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "set_correct_content_type_for_S3_assets.vcl"
                ).read_text(),
                name="Set correct Content-type for S3 assets",
                type="fetch",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "clean_response_headers_and_handle_404_on_delivery.vcl"
                ).read_text(),
                name="Clean response headers and handle 404 on delivery",
                type="deliver",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("reroute_redirects.vcl").read_text(),
                name="Reroute Redirects",
                type="error",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath(
                    "large_object_segmented_caching.vcl"
                ).read_text(),
                name="Segmented Caching",
                type="recv",
            ),
            fastly.ServiceVclSnippetArgs(
                content=snippets_dir.joinpath("strip_cookies_and_authorization.vcl").read_text(),
                name="Strip cookies and authorization.",
                type="recv",
            ),
        ],
        logging_https=[
            fastly.ServiceVclLoggingHttpArgs(
                url=Output.all(fqdn=vector_log_proxy_fqdn).apply(
                    lambda fqdn: "https://{fqdn}".format(**fqdn)
                ),
                name=f"ocw-{purpose}-{stack_info.env_suffix}-https-logging-args",
                content_type="application/json",
                format=build_fastly_log_format_string(
                    additional_static_fields={
                        "application": "open-courseware",
                        "environment": f"ocw-{stack_info.env_suffix}",
                        # service will be applied by the vector-log-proxy
                    }
                ),
                format_version=2,
                header_name="Authorization",
                header_value=f"Basic {encoded_fastly_proxy_credentials}",
                json_format="0",
                method="POST",
                request_max_bytes=ONE_MEGABYTE_BYTE,
            ),
        ],
        logging_s3s=[
            fastly.ServiceVclLoggingS3Args(
                bucket_name=fastly_access_logging_bucket["bucket_name"],
                name=f"ocw-{purpose}-{stack_info.env_suffix}-s3-logging-args",
                format=build_fastly_log_format_string(additional_static_fields={}),
                gzip_level=3,
                message_type="blank",
                path=f"/ocw/{stack_info.env_suffix}/{purpose}/",
                redundancy="standard",
                s3_iam_role=fastly_access_logging_iam_role["role_arn"],
            ),
        ],
        stale_if_error=True,
        opts=ResourceOptions(
            protect=True,
        ).merge(fastly_provider),
    )

    fastly.ServiceDictionaryItems(
        f"ocw-{purpose}-{stack_info.env_suffix}",
        service_id=servicevcl_backend.id,
        dictionary_id=servicevcl_backend.dictionaries[0].dictionary_id,
        items=json.load(open("redirect_dict.json")),  # noqa: PTH123
        manage_items=True,
        opts=ResourceOptions(protect=True).merge(fastly_provider),
    )

    fastly_distributions[purpose] = servicevcl_backend

    for domain in site_domains[purpose]:
        # If it's a 3 level domain then it's rooted at MIT.edu which means that we are
        # creating an Apex record in Route53. This means that we have to use an A
        # record. If it's deeper than 3 levels then it's a subdomain of ocw.mit.edu and
        # we can use a CNAME.
        record_type = "A" if len(domain.split(".")) == 3 else "CNAME"  # noqa: PLR2004
        record_value = (
            [str(addr) for addr in FASTLY_A_TLS_1_2]
            if record_type == "A"
            else [FASTLY_CNAME_TLS_1_3]
        )
        route53.Record(
            f"ocw-site-dns-record-{domain}",
            name=domain,
            type=record_type,
            ttl=FIVE_MINUTES,
            records=record_value,
            zone_id=ocw_zone["id"],
        )

export(
    "ocw_site_buckets",
    {
        "buckets": [
            draft_bucket_name,
            draft_backup_bucket_name,
            draft_offline_bucket_name,
            live_bucket_name,
            live_backup_bucket_name,
            live_offline_bucket_name,
        ],
        "policy": s3_bucket_iam_policy.name,
        "fastly_draft_service_id": fastly_distributions["draft"].id,
        "fastly_live_service_id": fastly_distributions["live"].id,
    },
)
