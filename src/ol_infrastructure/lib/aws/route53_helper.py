from typing import Text

import boto3
import pulumi

route53_client = boto3.client("route53")


def zone_opts(domain: Text) -> pulumi.ResourceOptions:
    """Look up and conditionally import an existing hosted zone.

    :param domain: The domain name to be looked up and optionally imported.  e.g. odl.mit.edu
    :type domain: Text

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
