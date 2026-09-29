"""Module for creating and managing S3 buckets that are not used by any applications."""

from pulumi import export
from pulumi_aws import route53, s3

from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.aws.s3_cloudfront_site import (
    S3ServerlessSite,
    S3ServerlessSiteConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.ol_types import BusinessUnit, Environment
from ol_infrastructure.lib.pulumi_helper import (
    make_stack_reference,
    parse_stack,
)

fifteen_minutes = 60 * 15
dns_stack = make_stack_reference(projects.DNS, "default")
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
        domains=["old.ocw.mit.edu"],
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

    # Durable archive for content copied out of buckets being decommissioned,
    # per the archive-then-delete cleanup process (see
    # https://github.com/mitodl/ol-infrastructure/issues/6089). Each
    # decommissioned bucket's contents land under a same-named prefix here
    # before the source bucket itself is deleted.
    archive_bucket_config = S3BucketConfig(
        bucket_name="ol-archive",
        tags={"OU": BusinessUnit.operations, "Environment": Environment.operations},
        versioning_enabled=True,
        # Everything landing here is already known-dead data, so skip the
        # Standard-tier waiting period Intelligent-Tiering would otherwise
        # impose (90 days to INTELLIGENT_TIERING, then another 90-180 days of
        # inactivity to reach an archive tier) and go straight to the
        # cheapest class.
        intelligent_tiering_enabled=False,
        lifecycle_rules=[
            s3.BucketLifecycleConfigurationRuleArgs(
                id="archive-immediately",
                status="Enabled",
                # Overrides S3's default exclusion of objects under 128KB
                # from lifecycle transitions -- this archive is full of
                # exactly that kind of small file (thumbnails, metadata,
                # etc.) and none of it should be left behind in Standard.
                filter=s3.BucketLifecycleConfigurationRuleFilterArgs(
                    object_size_greater_than=0,
                ),
                transitions=[
                    s3.BucketLifecycleConfigurationRuleTransitionArgs(
                        days=0,
                        storage_class="DEEP_ARCHIVE",
                    )
                ],
            )
        ],
    )
    archive_bucket = OLBucket("ol-archive", archive_bucket_config)
    export("ol_archive_bucket", archive_bucket.bucket_v2.bucket)
