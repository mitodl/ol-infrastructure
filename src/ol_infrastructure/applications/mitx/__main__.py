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

mitx_zone_id = dns_stack.get_output("mitx_zone_id")
mfe_app_learning_config = S3ServerlessSiteConfig(
    site_name=f"mfe-app-learning-{mitx_environment}",
    domains=[f"mfe-app-learning-{mitx_environment}.mitx.mit.edu"],
    bucket_name=f"mitx-mfe-app-learning-{mitx_environment}",
    tags=aws_config.tags,
    site_index="index.html",
    cloudfront_price_class=CloudfrontPriceClass.us_eu,
)
mfe_app_learning = S3ServerlessSite(site_config=mfe_app_learning_config)
mfe_app_learning_domain = route53.Record(
    f"mfe-app-learning-domain-{mitx_environment}",
    name=mfe_app_learning_config.domains[0],
    type="CNAME",
    ttl=fifteen_minutes,
    records=[mfe_app_learning.cloudfront_distribution.domain_name],
    zone_id=mitx_zone_id,
)

export("mfe_app_learning_bucket", mfe_app_learning.site_bucket.bucket)
export("xpro_certs_distribution_id", mfe_app_learning.cloudfront_distribution.id)
export("xpro_certs_acm_cname", mfe_app_learning.site_tls.domain_name)
