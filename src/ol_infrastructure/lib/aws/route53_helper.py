from functools import lru_cache

import boto3
import pulumi
import pulumi_fastly as fastly
from pulumi_aws import route53
from pulumi_aws.acm.outputs import CertificateDomainValidationOption

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
    best_zone: str | None = None
    for zone in zones_by_domain:
        if (domain == zone or domain.endswith("." + zone)) and (
            best_zone is None or len(zone) > len(best_zone)
        ):
            best_zone = zone
    if best_zone is None:
        return None
    return zones_by_domain[best_zone].split("/")[-1]


def zone_opts(domain: str) -> pulumi.ResourceOptions:
    """Look up and conditionally import an existing hosted zone.

    :param domain: The domain name to be looked up and optionally imported.  e.g.
        odl.mit.edu
    :type domain: str

    :returns: A Pulumi ResourceOptions object that allows for importing unmanaged zones

    :rtype: pulumi.ResourceOptions
    """
    zones = route53_client.list_hosted_zones_by_name(DNSName=domain).get(
        "HostedZones", []
    )

    if not zones:
        # Zone doesn't exist yet, will be created by Pulumi
        return pulumi.ResourceOptions()

    zone = zones[0]
    if zone["Name"].rstrip(".") == domain:
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
                import_=zone_id, ignore_changes=["tags", "tagsAll", "comment"]
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


def route53_record_opts(
    zone_id: str,
    record_name: str,
    record_type: str,
    *,
    managed: bool,
) -> pulumi.ResourceOptions:
    """Return ResourceOptions for a Route53 record resource.

    If managed is True, returns opts with delete_before_replace (already in Pulumi
    state, no import needed). If managed is False, boto3 is used to check whether
    the record exists. If it does, import opts are returned so Pulumi brings it into
    state on the next up. If it does not exist, Pulumi will create it.

    After a successful ``pulumi up`` that imports the record, set ``managed: true``
    in the stack config so subsequent runs skip the boto3 call.

    :param zone_id: Route53 hosted zone ID without the ``/hostedzone/`` prefix
    :param record_name: The DNS record name, e.g. ``mail.learn.mit.edu``
    :param record_type: The DNS record type, e.g. ``TXT``, ``MX``, ``CNAME``
    :param managed: Whether the record is already under Pulumi management

    :returns: ResourceOptions with import_ set if the record exists and is not yet
        managed
    :rtype: pulumi.ResourceOptions
    """
    if managed:
        return pulumi.ResourceOptions(delete_before_replace=True)

    resp = route53_client.list_resource_record_sets(
        HostedZoneId=zone_id,
        StartRecordName=record_name,
        StartRecordType=record_type,
        MaxItems="1",
    )
    records = resp.get("ResourceRecordSets", [])
    if records:
        existing = records[0]
        if (
            existing["Name"].rstrip(".") == record_name.rstrip(".")
            and existing["Type"] == record_type
        ):
            return pulumi.ResourceOptions(
                import_=f"{zone_id}_{record_name}_{record_type}",
                delete_before_replace=True,
            )
    return pulumi.ResourceOptions(delete_before_replace=True)


def acm_certificate_validation_records(
    validation_options: list[CertificateDomainValidationOption],
    cert_name: str,
    zone_id: str,
    opts: pulumi.ResourceOptions | None = None,
) -> list[route53.Record]:
    records_array = []
    for index, validation in enumerate(validation_options):
        records_array.append(
            route53.Record(
                f"{cert_name}-acm-cert-validation-route53-record-{index}",
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
        if zone_id := lookup_zone_id_from_domain(challenge.record_name):
            records_array.append(
                route53.Record(
                    f"fastly-cert-validation-route53-record-{challenge.record_name}-{index}",
                    name=challenge.record_name,
                    zone_id=zone_id,
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
    return domain in zones
