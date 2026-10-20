"""Module for creating and managing S3 buckets that are not used by any applications."""

from pulumi import ResourceOptions, export
from pulumi_aws import cloudwatch, route53, s3

from ol_infrastructure.components.aws.s3 import OLBucket, S3BucketConfig
from ol_infrastructure.components.aws.s3_cloudfront_site import (
    S3ServerlessSite,
    S3ServerlessSiteConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.monitoring_helper import get_monitoring_sns_arn
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
    # This bucket is a live CloudFront origin, so its objects must stay in
    # instantly-retrievable tiers. Intelligent-Tiering without a bucket-level
    # BucketIntelligentTieringConfiguration only uses the Frequent, Infrequent
    # and Archive Instant Access tiers, all of which serve without a restore.
    # Never add a BucketIntelligentTieringConfiguration with ARCHIVE_ACCESS or
    # DEEP_ARCHIVE_ACCESS here: one was applied to this bucket outside Pulumi,
    # archived ~28 GB of the site, and course pages under /courses/ then
    # returned 403 InvalidObjectState.
    s3.BucketLifecycleConfiguration(
        "ocw-legacy-bucket-lifecycle",
        bucket=ocw_legacy_site.site_bucket.id,
        transition_default_minimum_object_size="all_storage_classes_128K",
        rules=[
            s3.BucketLifecycleConfigurationRuleArgs(
                id="intelligent-tiering-transition",
                status="Enabled",
                filter=s3.BucketLifecycleConfigurationRuleFilterArgs(prefix=""),
                transitions=[
                    s3.BucketLifecycleConfigurationRuleTransitionArgs(
                        days=0,
                        storage_class="INTELLIGENT_TIERING",
                    )
                ],
            )
        ],
        opts=ResourceOptions(parent=ocw_legacy_site),
    )
    # Detect archived objects directly, since uptime checks can't: CloudFront
    # caches for at most 7 days, so a probe's cache misses still reach S3 often
    # enough that the page it fetches never goes cold. It only ever sees that
    # one page, never the ones that did.
    # S3 publishes these storage metrics once a day, and omits the datapoint
    # when a tier holds nothing.
    for storage_type, tier_name in (
        ("IntelligentTieringAAStorage", "archive-access"),
        ("IntelligentTieringDAAStorage", "deep-archive-access"),
    ):
        cloudwatch.MetricAlarm(
            f"ocw-legacy-bucket-{tier_name}-alarm",
            name=f"ocw-legacy-site-content-archive-{tier_name}-objects",
            alarm_description=(
                "Objects in ocw-legacy-site-content-archive are in an "
                "Intelligent-Tiering archive tier and return 403 "
                "InvalidObjectState on old.ocw.mit.edu until restored. Remove any "
                "BucketIntelligentTieringConfiguration on the bucket, then restore "
                "the objects with an S3 Batch Operations restore job."
            ),
            namespace="AWS/S3",
            metric_name="BucketSizeBytes",
            dimensions={
                "BucketName": ocw_legacy_site_config.bucket_name,
                "StorageType": storage_type,
            },
            statistic="Maximum",
            period=60 * 60 * 24,
            evaluation_periods=1,
            comparison_operator="GreaterThanThreshold",
            threshold=0,
            treat_missing_data="notBreaching",
            alarm_actions=[get_monitoring_sns_arn("warning")],
            ok_actions=[get_monitoring_sns_arn("warning")],
            tags=ocw_legacy_site_config.tags,
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
        bucket_name="ol-s3-archive",
        tags={"OU": BusinessUnit.operations, "Environment": Environment.operations},
        versioning_enabled=True,
        # Everything landing here is already known-dead data, so skip the
        # Standard-tier waiting period Intelligent-Tiering would otherwise
        # impose (90 days to INTELLIGENT_TIERING, then another 90-180 days of
        # inactivity to reach an archive tier) and go straight to the
        # cheapest class.
        intelligent_tiering_enabled=False,
        # Restores S3's pre-September-2024 default so that objects under
        # 128KB -- including zero-byte objects -- still transition to
        # DEEP_ARCHIVE below, rather than being silently left behind in
        # Standard forever. (A per-rule `ObjectSizeGreaterThan` filter can't
        # cover this: it's an exclusive lower bound, so even a value of `0`
        # excludes zero-byte objects.)
        transition_default_minimum_object_size="varies_by_storage_class",
        lifecycle_rules=[
            s3.BucketLifecycleConfigurationRuleArgs(
                id="archive-immediately",
                status="Enabled",
                transitions=[
                    s3.BucketLifecycleConfigurationRuleTransitionArgs(
                        days=0,
                        storage_class="DEEP_ARCHIVE",
                    )
                ],
                # Defensive: if this bucket is ever written to again for the
                # same destination key (re-running a migration, a correction,
                # etc.), the version it displaces becomes noncurrent here.
                # Without this, that copy would stay in Standard indefinitely.
                noncurrent_version_transitions=[
                    s3.BucketLifecycleConfigurationRuleNoncurrentVersionTransitionArgs(
                        noncurrent_days=0,
                        storage_class="DEEP_ARCHIVE",
                    )
                ],
            )
        ],
    )
    archive_bucket = OLBucket("ol-s3-archive", archive_bucket_config)
    export("ol_archive_bucket", archive_bucket.bucket_v2.bucket)
