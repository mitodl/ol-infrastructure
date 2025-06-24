from typing import Literal

from pulumi import Config, StackReference
from pulumi_aws import route53

from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit
from ol_infrastructure.lib.pulumi_helper import parse_stack

xpro_dns_config = Config("xpro_dns")
stack_info = parse_stack()
dns_stack = StackReference("infrastructure.aws.dns")
xpro_zone = dns_stack.require_output("xpro")
FIFTEEN_MINUTES = 60 * 15
aws_tags = AWSBase(tags={"OU": BusinessUnit.xpro, "Environment": "xpro"}).tags


def xpro_partner_record(
    subdomain: str,
    record: list[str] | str,
    record_type: Literal["A", "CNAME"] = "A",
) -> route53.Record:
    name_base = "{}.xpro.mit.edu".format
    return route53.Record(
        subdomain,
        name=name_base(subdomain),
        type=record_type,
        ttl=FIFTEEN_MINUTES,
        records=record if isinstance(record, list) else [record],
        zone_id=xpro_zone["id"],
    )


xpro_partner_record("digitalcourses-ga", "63.34.97.95")

xpro_partner_record("exec-ed", "18.136.29.151")

xpro_partner_record(
    "executive-ed",
    record="mitxpro.cloudflare.admissions.emeritus.org",
    record_type="CNAME",
)

xpro_partner_record(
    "globalalumni",
    record="54.72.183.79",
)

xpro_partner_record(
    "globalalumnicampus",
    record="99.81.253.115",
)

xpro_partner_record(
    "globalcourses",
    record="52.213.212.150",
)

xpro_partner_record(
    "pay.spanish",
    record="108.128.56.156",
)

xpro_partner_record(
    "portuguese",
    record="74.208.30.97",
)

xpro_partner_record(
    "sl-onlinetraining",
    record="dnblt0xpgmsqb.cloudfront.net",
    record_type="CNAME",
)

xpro_partner_record(
    "spanish",
    record="108.128.56.156",
)

xpro_partner_record(
    "spanishcourses",
    record="162.255.85.253",
)
