"""Module for creating and managing static websites hosted in S3 and delivered through Cloudfront."""  # noqa: E501

import json
from enum import Enum

from pulumi import ComponentResource, ResourceOptions
from pulumi_aws import acm, cloudfront, s3

from ol_infrastructure.lib.ol_types import AWSBase


class CloudfrontPriceClass(str, Enum):
    """Valid price classes for CloudFront to control tradeoffs of price vs. latency for global visitors."""  # noqa: E501

    # For more details on price class refer to below link and search for PriceClass
    # https://docs.aws.amazon.com/cloudfront/latest/APIReference/API_DistributionConfig.html
    # NB: POP == Points Of Presence which is where CDN edge servers are located
    us_eu = "PriceClass_100"  # POPs in US, Canada, Europe, and Israel
    exclude_au_sa = (  # POPs in all supported geos except South America and Australia  # noqa: E501
        "PriceClass_200"
    )
    all_geos = "PriceClass_All"  # POPs in all supoprted geos


class S3ServerlessSiteConfig(AWSBase):
    """Configuration object for customizing a static site hosted with S3 and Cloudfront."""  # noqa: E501

    site_name: str
    domains: list[str]
    bucket_name: str
    site_index: str = "index.html"
    cloudfront_price_class: CloudfrontPriceClass = CloudfrontPriceClass.us_eu


class S3ServerlessSite(ComponentResource):
    """A Pulumi component for constructing the resources to host a static website
    using S3 and Cloudfront.
    """

    def __init__(
        self, site_config: S3ServerlessSiteConfig, opts: ResourceOptions | None
    ):
        """Create an S3 bucket, ACM certificate, and Cloudfront distribution for
            hosting a static site.

        :param site_config: Configuration object for customizing the component
        :type site_config: S3ServerlessSiteConfig

        :param opts: Pulumi resource options
        :type opts: ResourceOptions

        :rtype: S3ServerlessSite
        """
        super().__init__(
            "ol:infrastructure:aws:S3ServerlessSite", site_config.site_name, None, opts
        )

        generic_resource_opts = ResourceOptions(parent=self).merge(opts)
        site_bucket_name = (f"{site_config.site_name}-bucket",)
        site_bucket_arn = f"arn:aws:s3:::{site_bucket_name}"

        self.site_bucket = s3.BucketV2(
            f"{site_config.site_name}-bucket",
            bucket=site_config.bucket_name,
            tags=site_config.tags,
        )

        site_bucket_ownership_controls = s3.BucketOwnershipControls(
            f"{site_bucket_name}-ownership-controls",
            bucket=self.site_bucket.id,
            rule=s3.BucketOwnershipControlsRuleArgs(
                object_ownership="BucketOwnerPreferred",
            ),
        )
        site_bucket_public_access = s3.BucketPublicAccessBlock(
            f"{site_bucket_name}-public-access",
            bucket=self.site_bucket.id,
            block_public_acls=False,
            block_public_policy=False,
            ignore_public_acls=False,
        )

        s3.BucketPolicy(
            f"{site_bucket_name}-policy",
            bucket=self.site_bucket.id,
            policy=json.dumps(
                {
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Sid": "PublicRead",
                            "Effect": "Allow",
                            "Principal": "*",
                            "Action": "s3:GetObject",
                            "Resource": [
                                f"{site_bucket_arn}/*",
                            ],
                        }
                    ],
                }
            ),
            opts=ResourceOptions(
                depends_on=[
                    site_bucket_public_access,
                    site_bucket_ownership_controls,
                ]
            ),
        )

        s3.BucketWebsiteConfigurationV2(
            f"{site_bucket_name}-website",
            bucket=self.site_bucket.id,
            index_document=s3.BucketWebsiteConfigurationV2IndexDocumentArgs(
                suffix="index.html",
            ),
            error_document=s3.BucketWebsiteConfigurationV2ErrorDocumentArgs(
                key="error.html",
            ),
        )

        _ = s3.BucketCorsConfigurationV2(
            f"{site_bucket_name}-cors",
            bucket=self.site_bucket.id,
            cors_rules=[
                s3.BucketCorsConfigurationV2CorsRuleArgs(
                    allowed_methods=["GET", "HEAD"],
                    allowed_origins=["*"],
                )
            ],
        )
        s3.BucketVersioningV2(
            f"{site_bucket_name}-versioning",
            bucket=self.site_bucket.id,
            versioning_configuration=s3.BucketVersioningV2VersioningConfigurationArgs(
                status="Enabled"
            ),
        )

        self.site_tls = acm.Certificate(
            f"{site_config.site_name}-tls",
            domain_name=site_config.domains[0],
            tags=site_config.merged_tags(
                {"Name": f"{site_config.site_name}-certificate"}
            ),
            validation_method="DNS",
            subject_alternative_names=site_config.domains[1:],
            opts=generic_resource_opts,
        )

        s3_origin_id = f"{site_config.site_name}-s3-origin"

        self.cloudfront_distribution = cloudfront.Distribution(
            f"{site_config.site_name}-cloudfront-distribution",
            aliases=site_config.domains,
            comment=f"Cloudfront distribution for {site_config.site_name}",
            default_cache_behavior={
                "allowedMethods": [
                    "GET",
                    "HEAD",
                    "OPTIONS",
                ],
                "cachedMethods": [
                    "GET",
                    "HEAD",
                ],
                "defaultTtl": 604800,
                "forwardedValues": {
                    "cookies": {
                        "forward": "none",
                    },
                    "queryString": False,
                },
                "maxTtl": 604800,
                "minTtl": 0,
                "targetOriginId": s3_origin_id,
                "viewerProtocolPolicy": "allow-all",
            },
            default_root_object="index.html",
            enabled=True,
            is_ipv6_enabled=True,
            origins=[
                {
                    "domain_name": self.site_bucket.bucket_regional_domain_name,
                    "originId": s3_origin_id,
                }
            ],
            price_class=site_config.cloudfront_price_class.value,
            restrictions={"geoRestriction": {"restrictionType": "none"}},
            tags=site_config.merged_tags(
                {"Name": f"{site_config.site_name}-cloudfront"}
            ),
            viewer_certificate={
                "acmCertificateArn": self.site_tls.arn,
                "sslSupportMethod": "sni-only",
            },
            opts=generic_resource_opts,
        )

        self.register_outputs({})
