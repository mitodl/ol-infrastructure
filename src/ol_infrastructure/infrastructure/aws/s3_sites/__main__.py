"""Module for creating and managing S3 buckets that are not used by any applications."""
from pulumi import export, StackReference, ResourceOptions, get_stack
from pulumi.config import get_config
from pulumi_aws import route53
from ol_infrastructure.components.aws.s3_cloudfront_site import S3ServerlessSiteConfig, S3ServerlessSite

fifteen_minutes = 60 * 15
dns_stack = StackReference('infrastructure.aws.dns')
stack = get_stack()
stack_name = stack.split('.')[-1]
env_suffix = stack_name.lower()

if 'production' in stack.lower():
    # Static site for hosting legacy course and program certificates from MIT xPro
    mitxpro_zone_id = dns_stack.get_output('mitxpro_legacy_zone_id')
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
        ttl=fifteen_minutes,
        records=[xpro_legacy_certs_site.cloudfront_distribution.domain_name],
        zone_id=mitxpro_zone_id
    )
    export('xpro_certs_bucket', xpro_legacy_certs_site.site_bucket.bucket)
    export('xpro_certs_distribution_id', xpro_legacy_certs_site.cloudfront_distribution.id)
    export('xpro_certs_acm_cname', xpro_legacy_certs_site.site_tls.domain_name)


# Static site for hosting beta releases of OCW content built with Hugo course publisher
mitodl_zone_id = dns_stack.get_output('odl_zone_id')
ocw_beta_site_config = S3ServerlessSiteConfig(
    site_name=f'ocw-beta-{env_suffix}-course-site',
    domains=[get_config('ocw_beta_site:domain')],
    bucket_name=f'ocw-beta-{env_suffix}-course-site',
    tags={'OU': 'open-courseware', 'Environment': f'ocw-{env_suffix}'}
)
ocw_beta_site = S3ServerlessSite(ocw_beta_site_config)
ocw_beta_domain = route53.Record(
    'ocw-beta-course-site-domain',
    name=ocw_beta_site_config.domains[0],
    type='CNAME',
    ttl=fifteen_minutes,
    records=[ocw_beta_site.cloudfront_distribution.domain_name],
    zone_id=mitodl_zone_id
)

export('ocw_beta_site_bucket', ocw_beta_site.site_bucket.bucket)
export('ocw_beta_site_distribution_id', ocw_beta_site.cloudfront_distribution.id)
export('ocw_beta_site_acm_cname', ocw_beta_site.site_tls.domain_name)
