from functools import lru_cache

import boto3
import pulumi
import pulumi_fastly as fastly
from pulumi_aws import route53
from pulumi_aws.acm.outputs import CertificateDomainValidationOption

from ol_infrastructure.lib.pulumi_helper import StackInfo

FIVE_MINUTES = 60 * 5
route53_client = boto3.client("route53")


@lru_cache
def zone_id_map() -> dict[str, str]:
    zones_by_domain = {}
    zone_kwargs: dict[str, str] = {"MaxItems": "100"}
    while True:
        zone_list = route53_client.list_hosted_zones_by_name(**zone_kwargs)
        if not zone_list["HostedZones"]:
            break
        for zone in zone_list["HostedZones"]:
            zones_by_domain[zone["Name"].strip(".")] = zone["Id"]
        if zone_list["IsTruncated"]:
            zone_kwargs = {
                "DNSName": zone_list["NextDNSName"],
                "HostedZoneId": zone_list["NextHostedZoneID"],
                "MaxItems": "100",
            }
        else:
            break
    return zones_by_domain


def lookup_zone_id_from_domain(domain: str) -> str | None:
    zones_by_domain = zone_id_map()
    for zone in zones_by_domain:
        if domain.endswith(zone):
            return zones_by_domain[zone].split("/")[-1]
    return None


def zone_opts(domain: str) -> pulumi.ResourceOptions:
    """Look up and conditionally import an existing hosted zone.

    :param domain: The domain name to be looked up and optionally imported.  e.g.
        odl.mit.edu
    :type domain: str

    :returns: A Pulumi ResourceOptions object that allows for importing unmanaged zones

    :rtype: pulumi.ResourceOptions
    """
    zone = route53_client.list_hosted_zones_by_name(DNSName=domain)["HostedZones"][0]
    if zone["Name"] == domain:
        zone_id = zone["Id"].split("/")[
            -1
        ]  # 'Id' attribute is of the form /hostedzone/<ZONE_ID>
        tags = route53_client.list_tags_for_resource(
            ResourceType="hostedzone", ResourceId=zone_id
        )
        if "pulumi_managed" in {tag["Key"] for tag in tags["ResourceTagSet"]["Tags"]}:
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
    else:
        opts = pulumi.ResourceOptions()
    return opts


def acm_certificate_validation_records(
    validation_options: list[CertificateDomainValidationOption],
    cert_name: str,
    zone_id: str,
    stack_info: StackInfo,
    opts: pulumi.ResourceOptions | None = None,
) -> list[route53.Record]:
    records_array = []
    for index, validation in enumerate(validation_options):
        records_array.append(
            route53.Record(
                f"{cert_name}{stack_info.env_prefix}-acm-cert-validation-route53-record-{index}",
                name=validation.resource_record_name,
                zone_id=zone_id,
                type=validation.resource_record_type,
                records=[validation.resource_record_value],
                ttl=FIVE_MINUTES,
                allow_overwrite=True,
                opts=opts,
            )
        )
    return records_array


def fastly_certificate_validation_records(
    validation_options: list[fastly.TlsSubscriptionManagedDnsChallengeArgs],
    opts: pulumi.ResourceOptions | None = None,
) -> list[route53.Record]:
    records_array = []
    for index, challenge in enumerate(validation_options):
        records_array.append(
            route53.Record(
                f"fastly-cert-validation-route53-record-{index}",
                name=challenge.record_name,
                zone_id=lookup_zone_id_from_domain(challenge.record_name),
                type=challenge.record_type,
                records=[challenge.record_value],
                ttl=FIVE_MINUTES,
                allow_overwrite=True,
                opts=opts,
            )
        )
    return records_array


def is_root_domain(domain: str) -> bool:
    zones = zone_id_map()
    try:
        zones[domain]
    except KeyError:
        return False
    return True
