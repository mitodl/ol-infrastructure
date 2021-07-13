from typing import List

import boto3
import pulumi
from pulumi_aws import route53
from pulumi_aws.acm.outputs import CertificateDomainValidationOption

FIVE_MINUTES = 60 * 5
route53_client = boto3.client("route53")


def zone_opts(domain: str) -> pulumi.ResourceOptions:
    """Look up and conditionally import an existing hosted zone.

    :param domain: The domain name to be looked up and optionally imported.  e.g.
        odl.mit.edu
    :type domain: str

    :returns: A Pulumi ResourceOptions object that allows for importing unmanaged zones

    :rtype: pulumi.ResourceOptions
    """
    zone = route53_client.list_hosted_zones_by_name(DNSName=domain)["HostedZones"][0]
    zone_id = zone["Id"].split("/")[
        -1
    ]  # 'Id' attribute is of the form /hostedzone/<ZONE_ID>
    tags = route53_client.list_tags_for_resource(
        ResourceType="hostedzone", ResourceId=zone_id
    )
    if "pulumi_managed" in {  # noqa: C412, WPS337
        tag["Key"] for tag in tags["ResourceTagSet"]["Tags"]
    }:
        opts = pulumi.ResourceOptions()
    else:
        opts = pulumi.ResourceOptions(
            import_=zone_id, ignore_changes=["tags", "comment"]
        )
        if not pulumi.runtime.is_dry_run():
            route53_client.change_tags_for_resource(
                ResourceType="hostedzone",
                ResourceId=zone_id,
                AddTags=[{"Key": "pulumi_managed", "Value": "true"}],
            )
    return opts


def acm_certificate_validation_records(
    validation_options: List[CertificateDomainValidationOption], zone_id: str
) -> List[route53.Record]:
    records_array = []
    for index, validation in enumerate(validation_options):
        records_array.append(
            route53.Record(
                f"edxapp-acm-cert-validation-route53-record-{index}",
                name=validation.resource_record_name,
                zone_id=zone_id,
                type=validation.resource_record_type,
                records=[validation.resource_record_value],
                ttl=FIVE_MINUTES,
                allow_overwrite=True,
            )
        )
    return records_array
