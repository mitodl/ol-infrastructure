"""Tests for the S3ServerlessSite component's bucket policy."""

import asyncio
import json
import os

# Set AWS environment variables before importing boto3-dependent modules
os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
os.environ["AWS_REGION"] = "us-east-1"
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"  # pragma: allowlist secret  # noqa: S105

import pulumi
import pytest

# Python 3.14+ compatibility: ensure event loop exists for set_mocks()
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ol_infrastructure.components.aws import acm as acm_component
from ol_infrastructure.components.aws.s3_cloudfront_site import (
    S3ServerlessSite,
    S3ServerlessSiteConfig,
)

BUCKET_NAME = "real-site-bucket-name"


class SiteMocks(pulumi.runtime.Mocks):
    """Echo inputs as outputs, adding the ARN a real Bucket would report."""

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        """Return the resource's inputs as its outputs."""
        outputs = dict(args.inputs)
        if args.typ == "aws:s3/bucket:Bucket":
            outputs["arn"] = f"arn:aws:s3:::{args.inputs['bucket']}"
        elif args.typ == "aws:acm/certificate:Certificate":
            # No validation records needed; the component iterates this list.
            outputs["domainValidationOptions"] = []
        return [f"{args.name}_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):  # noqa: ARG002
        """Return an empty result for any provider function call."""
        return {}


@pytest.fixture(autouse=True)
def site_mocks(monkeypatch):
    """Install mocks, and stub ACMCertificate's real boto3 Route53 zone lookup."""
    monkeypatch.setattr(
        acm_component, "lookup_zone_id_from_domain", lambda _domain: "Z123"
    )
    pulumi.runtime.set_mocks(SiteMocks())


@pulumi.runtime.test
def test_bucket_policy_grants_read_on_the_real_bucket():
    """The policy must name the bucket it is attached to, not the resource name.

    S3 rejects a policy whose Resource is a different bucket with
    MalformedPolicy, which is what the site_name-derived ARN produced.
    """
    site = S3ServerlessSite(
        S3ServerlessSiteConfig(
            site_name="test-site",
            domains=["site.example.com"],
            bucket_name=BUCKET_NAME,
            tags={"OU": "operations", "Environment": "operations"},
        )
    )

    def check(policy_json):
        # Compare the whole policy so a widened Action or Principal also fails.
        assert json.loads(policy_json) == {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "PublicRead",
                    "Effect": "Allow",
                    "Principal": "*",
                    "Action": "s3:GetObject",
                    "Resource": [f"arn:aws:s3:::{BUCKET_NAME}/*"],
                }
            ],
        }

    return site.site_bucket_policy.policy.apply(check)
