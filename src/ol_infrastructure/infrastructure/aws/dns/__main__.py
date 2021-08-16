from pulumi import export
from pulumi_aws import route53

from ol_infrastructure.lib.aws.route53_helper import zone_opts
from ol_infrastructure.lib.ol_types import AWSBase

mitxpro_legacy_dns_name = "mitxpro.mit.edu"
mitxpro_legacy_opts = zone_opts(mitxpro_legacy_dns_name)
mitxpro_legacy_dns_zone = route53.Zone(
    "mitxpro_legacy_subdomain",
    name=mitxpro_legacy_dns_name,
    comment="DNS Zone for legacy xPro resources",
    tags=AWSBase(tags={"OU": "mitxpro", "Environment": "operations"}).tags,
    opts=mitxpro_legacy_opts,
)

odl_dns_name = "odl.mit.edu"
odl_opts = zone_opts(odl_dns_name)
odl_dns_zone = route53.Zone(
    "mitodl_subdomain",
    name=odl_dns_name,
    comment="DNS Zone used for ODL resources",
    tags=AWSBase(tags={"OU": "operations", "Environment": "operations"}).tags,
    opts=odl_opts,
)

mitx_dns_name = "mitx.mit.edu"
mitx_opts = zone_opts(mitx_dns_name)
mitx_dns_zone = route53.Zone(
    "mitx_subdomain",
    name=mitx_dns_name,
    comment="DNS Zone used for MITx resources",
    tags=AWSBase(tags={"OU": "residential", "Environment": "mitx"}).tags,
    opts=mitx_opts,
)

mitxonline_dns_name = "mitxonline.mit.edu"
mitxonline_opts = zone_opts(mitxonline_dns_name)
mitxonline_dns_zone = route53.Zone(
    "mitxonline_subdomain",
    name=mitxonline_dns_name,
    comment="DNS Zone used for MITx Online resources",
    tags=AWSBase(tags={"OU": "mitxonline", "Environment": "mitxonline"}).tags,
    opts=mitxonline_opts,
)

export("mitxpro_legacy_zone_id", mitxpro_legacy_dns_zone.id)
export("odl_zone_id", odl_dns_zone.id)
export("mitx_zone_id", mitx_dns_zone.id)
export("mitxonline", {"id": mitxonline_dns_zone.id, "domain": mitxonline_dns_zone.name})
