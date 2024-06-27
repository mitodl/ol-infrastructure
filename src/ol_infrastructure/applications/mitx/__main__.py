from pulumi import StackReference, export
from pulumi_aws import route53

from ol_infrastructure.components.aws.s3_cloudfront_site import (
    CloudfrontPriceClass,
    S3ServerlessSite,
    S3ServerlessSiteConfig,
)
from ol_infrastructure.lib.ol_types import Apps, AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

fifteen_minutes = 60 * 15
dns_stack = StackReference("infrastructure.aws.dns")
stack_info = parse_stack()
mitx_environment = f"mitx-{stack_info.env_suffix}"
aws_config = AWSBase(
    tags={
        "OU": "mitx",
        "Environment": mitx_environment,
        "Application": Apps.mitx_edx.value,
    }
)

mitx_zone_id = dns_stack.get_output("mitx")["id"]
mitx_mfe_config = S3ServerlessSiteConfig(
    site_name=f"mitx-mfe-{mitx_environment}",
    domains=[f"static-{mitx_environment}.mitx.mit.edu"],
    bucket_name=f"mitx-mfe-{mitx_environment}",
    tags=aws_config.tags,
    site_index="index.html",
    cloudfront_price_class=CloudfrontPriceClass.us_eu,
)
mitx_mfe = S3ServerlessSite(site_config=mitx_mfe_config)
mitx_mfe_domain = route53.Record(
    f"mitx-mfe-domain-{mitx_environment}",
    name=mitx_mfe_config.domains[0],
    type="CNAME",
    ttl=fifteen_minutes,
    records=[mitx_mfe.cloudfront_distribution.domain_name],
    zone_id=mitx_zone_id,
)

export("mitx_mfe_bucket", mitx_mfe.site_bucket.bucket)
export("mitx_mfe_distribution_id", mitx_mfe.cloudfront_distribution.id)
export("mitx_mfe_acm_cname", mitx_mfe.site_tls.domain_name)
