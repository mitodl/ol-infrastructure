import json
from pathlib import Path

import pulumi_fastly as fastly
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import iam, route53, s3

from bridge.lib.constants import FASTLY_A_TLS_1_2, FASTLY_CNAME_TLS_1_3
from bridge.lib.magic_numbers import (
    DEFAULT_HTTPS_PORT,
    FIVE_MINUTES,
    HTTP_STATUS_NOT_FOUND,
    HTTP_STATUS_OK,
    HTTP_STATUS_SERVICE_UNAVAILABLE,
    SECONDS_IN_ONE_DAY,
)
from ol_infrastructure.lib.aws.iam_helper import lint_iam_policy
from ol_infrastructure.lib.fastly import get_fastly_provider, fastly_log_format_string
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

monitoring_stack = StackReference("infrastructure.monitoring.Production")
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

draft_backup_bucket_name = f"ocw-content-backup-draft-{stack_info.env_suffix}"
draft_backup_bucket_arn = f"arn:aws:s3:::{draft_backup_bucket_name}"
live_backup_bucket_name = f"ocw-content-backup-live-{stack_info.env_suffix}"
live_backup_bucket_arn = f"arn:aws:s3:::{live_backup_bucket_name}"

draft_offline_bucket_name = f"ocw-content-offline-draft-{stack_info.env_suffix}"
draft_offline_bucket_arn = f"arn:aws:s3:::{draft_offline_bucket_name}"
live_offline_bucket_name = f"ocw-content-offline-live-{stack_info.env_suffix}"
live_offline_bucket_arn = f"arn:aws:s3:::{live_offline_bucket_name}"

draft_bucket = s3.Bucket(
    draft_bucket_name,
    bucket=draft_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{draft_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)
draft_backup_bucket = s3.Bucket(
    draft_backup_bucket_name,
    bucket=draft_backup_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{draft_backup_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
    versioning=s3.BucketVersioningArgs(enabled=True),
    lifecycle_rules=[
        s3.BucketLifecycleRuleArgs(
            enabled=True,
            noncurrent_version_expiration=s3.BucketLifecycleRuleNoncurrentVersionExpirationArgs(  # noqa: E501
                days=90,
            ),
        )
    ],
)
draft_offline_bucket = s3.Bucket(
    draft_offline_bucket_name,
    bucket=draft_offline_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{draft_offline_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)

live_bucket = s3.Bucket(
    live_bucket_name,
    bucket=live_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{live_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
)
live_backup_bucket = s3.Bucket(
    live_backup_bucket_name,
    bucket=live_backup_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{live_backup_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
    versioning=s3.BucketVersioningArgs(enabled=True),
    lifecycle_rules=[
        s3.BucketLifecycleRuleArgs(
            enabled=True,
            noncurrent_version_expiration=s3.BucketLifecycleRuleNoncurrentVersionExpirationArgs(  # noqa: E501
                days=90,
            ),
        )
    ],
)
live_offline_bucket = s3.Bucket(
    live_offline_bucket_name,
    bucket=live_offline_bucket_name,
    tags=aws_config.tags,
    acl="public-read",
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"{live_offline_bucket_arn}/*"],
                }
            ],
        }
    ),
    cors_rules=[{"allowedMethods": ["GET", "HEAD"], "allowedOrigins": ["*"]}],
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
fastly_distributions: dict[str, fastly.ServiceVcl] = {}
for purpose in ("draft", "live"):
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
                name="not course media or old akamai",
                statement='req.url.path !~ "^/coursemedia" && req.url.path !~ "^/ans\\d+"',  # noqa: E501
                type="REQUEST",
            ),
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for robots.txt",
                priority=0,
                statement='req.url.path == "/robots.txt"',
                type="REQUEST",
            ),
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for 503 page",
                priority=0,
                statement="beresp.status == 503",
                type="CACHE",
            ),
            fastly.ServiceVclConditionArgs(
                name="Generated by synthetic response for 404 page",
                statement="beresp.status == 404",
                type="CACHE",
            ),
            fastly.ServiceVclConditionArgs(
                name="is old Akamai file",
                statement='req.url.path ~ "^/ans\\d+" && req.url.path !~ "/ans7870/21f/21f.027"',  # noqa: E501
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
        headers=[
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
                cache_condition="Generated by synthetic response for 404 page",
                content=project_dir.joinpath("404.html").read_text(),
                content_type="text/html",
                name="Generated by synthetic response for 404 page",
                response="Not Found",
                status=HTTP_STATUS_NOT_FOUND,
            ),
            fastly.ServiceVclResponseObjectArgs(
                cache_condition="Generated by synthetic response for 503 page",
                content=project_dir.joinpath("503.html").read_text(),
                content_type="text/html",
                name="Generated by synthetic response for 503 page",
                response="Service Unavailable",
                status=HTTP_STATUS_SERVICE_UNAVAILABLE,
            ),
            fastly.ServiceVclResponseObjectArgs(
                content=snippets_dir.joinpath(robots_file).read_text(),
                content_type="text/plain",
                name="Generated by synthetic response for robots.txt",
                request_condition="Generated by synthetic response for robots.txt",
                status=HTTP_STATUS_OK,
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
        ],
        logging_s3s=[
            fastly.ServiceVclLoggingS3Args(
                bucket_name=fastly_access_logging_bucket["bucket_name"],
                name=f"ocw-{purpose}-{stack_info.env_suffix}-logging-args",
                format=fastly_log_format_string,
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
        items=json.load(open("redirect_dict.json")),  # noqa: SIM115
        manage_items=True,
        opts=ResourceOptions(protect=True).merge(fastly_provider),
    )

    fastly_distributions[purpose] = servicevcl_backend

    for domain in site_domains[purpose]:
        # If it's a 3 level domain then it's rooted at MIT.edu which means that we are
        # creating an Apex record in Route53. This means that we have to use an A
        # record. If it's deeper than 3 levels then it's a subdomain of ocw.mit.edu and
        # we can use a CNAME.
        record_type = "A" if len(domain.split(".")) == 3 else "CNAME"
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
