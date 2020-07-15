"""Module for creating and managing S3 buckets that are not used by any applications."""
from pulumi import export

from ol_infrastructure.components.aws.s3_cloudfront_site import S3ServerlessSiteConfig, S3ServerlessSite

xpro_legacy_certs_site_config = S3ServerlessSiteConfig(
    site_name='xpro-legacy-certificates',
    domains=['certificates.mitxpro.mit.edu'],
    bucket_name='mitxpro-legacy-certificates',
    tags={'OU': 'mitxpro', 'Environment': 'operations'}
)

xpro_legacy_certs_site = S3ServerlessSite(xpro_legacy_certs_site_config)

export('xpro_certs_bucket', xpro_legacy_certs_site.site_bucket.bucket)
export('xpro_certs_distribution_id', xpro_legacy_certs_site.cloudfront_distribution.id)
