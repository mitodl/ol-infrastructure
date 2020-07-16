"""Module for creating and managing S3 buckets that are not used by any applications."""
from pulumi import export, StackReference
from pulumi_aws import route53
from ol_infrastructure.components.aws.s3_cloudfront_site import S3ServerlessSiteConfig, S3ServerlessSite

dns_stack = StackReference('infrastructure.aws.dns')
mitxpro_zone_id = dns_stack.get_output('mitxpro_zone_id')
xpro_legacy_certs_site_config = S3ServerlessSiteConfig(
    site_name='xpro-legacy-certificates',
    domains=['certificates.mitxpro.mit.edu'],
    bucket_name='mitxpro-legacy-certificates',
    tags={'OU': 'mitxpro', 'Environment': 'operations'}
)
xpro_legacy_certs_site = S3ServerlessSite(xpro_legacy_certs_site_config)
xpro_legacy_certs_domain = route53.Record(
    'xpro-legacy-certificates-domain',
    name=xpro_legacy_certs_site_config.domains[0],
    type='CNAME',
    records=xpro_legacy_certs_site.cloudfront_distribution.domain_name,
    zone_id=mitxpro_zone_id
)

export('xpro_certs_bucket', xpro_legacy_certs_site.site_bucket.bucket)
export('xpro_certs_distribution_id', xpro_legacy_certs_site.cloudfront_distribution.id)
export('xpro_certs_acm_cname', xpro_legacy_certs_site.site_tls.domain_name)
