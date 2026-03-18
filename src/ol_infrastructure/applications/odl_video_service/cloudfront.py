"""CloudFront distribution and Origin Access Identities for ODL Video Service.

This module manages the CloudFront distribution that serves S3-hosted video
assets (main bucket, transcoded video, thumbnails, and subtitles).

Resources were originally created outside of Pulumi and imported via:
    pulumi import --file <import.json> --out cloudfront.py --protect=false

Import IDs per environment:
  Production: Distribution=E1M3I29KI2GTVW, OAI=E7DYAE230Z572
  CI:         Distribution=E1WW306SK0AT0U, OAI=E3KTEO47PV050U
  QA:         Distribution=E1KA122GOS192Z, OAI(primary)=E3C52WDUOI657Y,
                                            OAI(transcoded)=E4WWZ6V050N8M
"""

from pulumi import Config, Output, ResourceOptions
from pulumi_aws import cloudfront

from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack

ovs_config = Config("ovs")
stack_info = parse_stack()
env_suffix = stack_info.env_suffix  # "production", "ci", "qa"

# QA uses "rc" (release-candidate) in resource names rather than "qa"
_env_variant_map: dict[str, str] = {
    "production": "production",
    "ci": "ci",
    "qa": "rc",
}
env_variant = _env_variant_map[env_suffix]

aws_config = AWSBase(
    tags={
        "OU": "odl-video",
        "Environment": f"applications_{env_suffix}",
    }
)

# Bucket names come from stack config
s3_bucket_name = ovs_config.require("s3_bucket_name")
s3_subtitle_bucket_name = ovs_config.require("s3_subtitle_bucket_name")
s3_thumbnail_bucket_name = ovs_config.require("s3_thumbnail_bucket_name")
s3_transcode_bucket_name = ovs_config.require("s3_transcode_bucket_name")

# ── Origin Access Identities ──────────────────────────────────────────────────

_oai_comment_map: dict[str, str] = {
    "production": "odl video service identity",
    "ci": "odl-video-service-ci.s3.amazonaws.com",
    "qa": "odl-video-service-rc.s3.amazonaws.com",
}

ovs_cloudfront_oai = cloudfront.OriginAccessIdentity(
    "ovs-cloudfront-oai",
    comment=_oai_comment_map[env_suffix],
)

oai_path: Output[str] = ovs_cloudfront_oai.id.apply(
    lambda oai_id: f"origin-access-identity/cloudfront/{oai_id}"
)

# QA has a second legacy OAI used only for the transcoded bucket origin.
# Production and CI use the same OAI for all four origins.
if env_suffix == "qa":
    ovs_cloudfront_transcoded_oai = cloudfront.OriginAccessIdentity(
        "ovs-cloudfront-transcoded-oai",
        comment="access-identity-odl-video-service-transcoded-rc",
    )
    transcoded_oai_path: Output[str] = ovs_cloudfront_transcoded_oai.id.apply(
        lambda oai_id: f"origin-access-identity/cloudfront/{oai_id}"
    )
else:
    transcoded_oai_path = oai_path

# ── Origin / cache-behavior helpers ──────────────────────────────────────────

# Origin IDs are logical names embedded in the CloudFront distribution state.
# All use the "S3-{bucket}" convention except QA's transcoded origin, which
# was created without the prefix (legacy quirk).
main_origin_id = f"S3-{s3_bucket_name}"
subtitle_origin_id = f"S3-{s3_subtitle_bucket_name}"
thumbnail_origin_id = f"S3-{s3_thumbnail_bucket_name}"
transcoded_origin_id = (
    s3_transcode_bucket_name  # QA: no "S3-" prefix
    if env_suffix == "qa"
    else f"S3-{s3_transcode_bucket_name}"
)

# QA's transcoded bucket was registered with an explicit-region endpoint.
transcoded_domain_name = (
    f"{s3_transcode_bucket_name}.s3.us-east-1.amazonaws.com"
    if env_suffix == "qa"
    else f"{s3_transcode_bucket_name}.s3.amazonaws.com"
)

# Shared settings for the four CORS-forwarding S3 cache behaviors
_s3_cors_behavior: dict[str, object] = {
    "allowed_methods": ["GET", "HEAD", "OPTIONS"],
    "cached_methods": ["GET", "HEAD", "OPTIONS"],
    "default_ttl": 86400,
    "forwarded_values": {
        "cookies": {"forward": "none"},
        "headers": [
            "Access-Control-Request-Headers",
            "Access-Control-Request-Method",
            "Origin",
        ],
        "query_string": False,
    },
    "max_ttl": 31536000,
    "viewer_protocol_policy": "redirect-to-https",
}

# ── Distribution ──────────────────────────────────────────────────────────────

# Logging is only enabled for production; CI and QA have no logging bucket.
logging_config = (
    {
        "bucket": "odl-video-service-cloudfront-logging-production.s3.amazonaws.com",
        "include_cookies": True,
    }
    if env_suffix == "production"
    else None
)

# Production serves all edge locations; CI/QA use US+Europe only (cost savings).
price_class = "PriceClass_All" if env_suffix == "production" else "PriceClass_100"

# QA has a custom ACM certificate; production and CI use the CloudFront default.
_viewer_cert_map: dict[str, dict[str, object]] = {
    "production": {
        "cloudfront_default_certificate": True,
        "minimum_protocol_version": "TLSv1",
    },
    "ci": {
        "cloudfront_default_certificate": True,
        "minimum_protocol_version": "TLSv1",
    },
    "qa": {
        "acm_certificate_arn": (
            "arn:aws:acm:us-east-1:610119931565:certificate/"
            "bb4cc15a-5fcb-46df-a5ae-bf9ca4115667"
        ),
        "minimum_protocol_version": "TLSv1.2_2021",
        "ssl_support_method": "sni-only",
    },
}

ovs_cloudfront_distribution = cloudfront.Distribution(
    "ovs-cloudfront-distribution",
    comment=f"odl-video-service-{env_variant}",
    default_cache_behavior={
        **_s3_cors_behavior,
        "target_origin_id": main_origin_id,
    },
    enabled=True,
    http_version="http2",
    is_ipv6_enabled=True,
    logging_config=logging_config,
    ordered_cache_behaviors=[
        {
            **_s3_cors_behavior,
            "path_pattern": "/transcoded*",
            "target_origin_id": transcoded_origin_id,
        },
        {
            **_s3_cors_behavior,
            "path_pattern": "/thumbnails*",
            "target_origin_id": thumbnail_origin_id,
        },
        {
            **_s3_cors_behavior,
            "path_pattern": "/subtitles*",
            "target_origin_id": subtitle_origin_id,
        },
        # Retranscode path uses the AWS-managed CachingOptimized policy
        # (658327ea-f89d-4fab-a63d-7e88639e58f6) instead of ForwardedValues.
        {
            "allowed_methods": ["GET", "HEAD"],
            "cache_policy_id": "658327ea-f89d-4fab-a63d-7e88639e58f6",
            "cached_methods": ["GET", "HEAD"],
            "compress": True,
            "path_pattern": "/retranscode*",
            "target_origin_id": transcoded_origin_id,
            "viewer_protocol_policy": "redirect-to-https",
        },
    ],
    origins=[
        {
            "domain_name": f"{s3_subtitle_bucket_name}.s3.amazonaws.com",
            "origin_id": subtitle_origin_id,
            "s3_origin_config": {"origin_access_identity": oai_path},
        },
        {
            "domain_name": f"{s3_thumbnail_bucket_name}.s3.amazonaws.com",
            "origin_id": thumbnail_origin_id,
            "s3_origin_config": {"origin_access_identity": oai_path},
        },
        {
            "domain_name": transcoded_domain_name,
            "origin_id": transcoded_origin_id,
            "s3_origin_config": {"origin_access_identity": transcoded_oai_path},
        },
        {
            "domain_name": f"{s3_bucket_name}.s3.amazonaws.com",
            "origin_id": main_origin_id,
            "s3_origin_config": {"origin_access_identity": oai_path},
        },
    ],
    price_class=price_class,
    restrictions={
        "geo_restriction": {"restriction_type": "none"},
    },
    tags=aws_config.merged_tags(
        {
            "Name": f"odl-video-service-{env_variant}",
        }
    ),
    viewer_certificate=_viewer_cert_map[env_suffix],
    opts=ResourceOptions(ignore_changes=["tags"]),
)
