"""Module for creating and managing S3 buckets that are not used by any applications."""

from pulumi import StackReference, export
from pulumi_aws import route53

from ol_infrastructure.components.aws.s3_cloudfront_site import (
    S3ServerlessSite,
    S3ServerlessSiteConfig,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack

fifteen_minutes = 60 * 15
dns_stack = StackReference("infrastructure.aws.dns")
stack_info = parse_stack()

if stack_info.env_suffix == "production":
    # Static site for hosting legacy course and program certificates from MIT xPro
    mitxpro_zone_id = dns_stack.get_output("mitxpro_legacy_zone_id")
    xpro_legacy_certs_site_config = S3ServerlessSiteConfig(
        site_name="xpro-legacy-certificates",
        domains=["certificates.mitxpro.mit.edu"],
        bucket_name="mitxpro-legacy-certificates",
        tags={"OU": "mitxpro", "Environment": "operations"},
    )
    xpro_legacy_certs_site = S3ServerlessSite(xpro_legacy_certs_site_config)
    xpro_legacy_certs_domain = route53.Record(
        "xpro-legacy-certificates-domain",
        name=xpro_legacy_certs_site_config.domains[0],
        type="CNAME",
        ttl=fifteen_minutes,
        records=[xpro_legacy_certs_site.cloudfront_distribution.domain_name],
        zone_id=mitxpro_zone_id,
    )
    export("xpro_certs_bucket", xpro_legacy_certs_site.site_bucket.bucket)
    export(
        "xpro_certs_distribution_id", xpro_legacy_certs_site.cloudfront_distribution.id
    )
    export("xpro_certs_acm_cname", xpro_legacy_certs_site.site_tls.domain_name)
    # Static site for hosting legacy OCW
    ocw_zone_id = dns_stack.get_output("ocw")["id"]
    ocw_legacy_site_config = S3ServerlessSiteConfig(
        site_name="ocw-legacy",
        domains=["ocw-legacy.ocw.mit.edu"],
        bucket_name="ocw-legacy-site-content-archive",
        site_index="index.htm",
        tags={"OU": "open-courseware", "Environment": "applications"},
    )
    ocw_legacy_site = S3ServerlessSite(ocw_legacy_site_config)
    ocw_legacy_domain = route53.Record(
        "ocw-legacy-domain",
        name=ocw_legacy_site_config.domains[0],
        type="CNAME",
        ttl=fifteen_minutes,
        records=[ocw_legacy_site.cloudfront_distribution.domain_name],
        zone_id=ocw_zone_id,
    )
    export("ocw_legacy_bucket", ocw_legacy_site.site_bucket.bucket)
    export("ocw_legacy_distribution_id", ocw_legacy_site.cloudfront_distribution.id)
    export("ocw_legacy_acm_cname", ocw_legacy_site.site_tls.domain_name)
