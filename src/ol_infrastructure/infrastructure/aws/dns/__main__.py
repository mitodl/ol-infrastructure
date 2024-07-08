from pulumi import export
from pulumi_aws import route53

from ol_infrastructure.lib.aws.route53_helper import zone_opts
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit, Environment

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

ol_dns_name = "ol.mit.edu"
ol_opts = zone_opts(odl_dns_name)
ol_dns_zone = route53.Zone(
    "mitol_subdomain",
    name=ol_dns_name,
    comment="DNS Zone used for Open Learning resources",
    tags=AWSBase(tags={"OU": "operations", "Environment": "operations"}).tags,
    opts=ol_opts,
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

xpro_dns_name = "xpro.mit.edu"
xpro_opts = zone_opts(xpro_dns_name)
xpro_dns_zone = route53.Zone(
    "xpro_subdomain",
    name=xpro_dns_name,
    comment="DNS Zone used for xPRO resources",
    tags=AWSBase(tags={"OU": "mitxpro", "Environment": "xpro"}).tags,
    opts=xpro_opts,
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

ocw_dns_name = "ocw.mit.edu"
ocw_opts = zone_opts(ocw_dns_name)
ocw_dns_zone = route53.Zone(
    "ocw_subdomain",
    name=ocw_dns_name,
    comment="DNS Zone used for OCW resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.ocw, "Environment": Environment.applications}
    ).tags,
    opts=ocw_opts,
)

learn_dns_name = "learn.mit.edu"
learn_opts = zone_opts(learn_dns_name)
learn_dns_zone = route53.Zone(
    "learn_subdomain",
    name=learn_dns_name,
    comment="DNS Zone used for mitopen resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=learn_opts,
)

podcasts_dns_name = "podcasts.mit.edu"
podcasts_opts = zone_opts(podcasts_dns_name)
podcasts_dns_zone = route53.Zone(
    "podcasts_subdomain",
    name=podcasts_dns_name,
    comment="DNS Zone used for mitopen resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=podcasts_opts,
)

podcast_dns_name = "podcast.mit.edu"
podcast_opts = zone_opts(podcast_dns_name)
podcast_dns_zone = route53.Zone(
    "podcast_subdomain",
    name=podcast_dns_name,
    comment="DNS Zone used for mitopen resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=podcast_opts,
)

export("mitxpro_legacy_zone_id", mitxpro_legacy_dns_zone.id)
export("odl_zone_id", odl_dns_zone.id)
export("mitxonline", {"id": mitxonline_dns_zone.id, "domain": mitxonline_dns_zone.name})
export("xpro", {"id": xpro_dns_zone.id, "domain": xpro_dns_zone.name})
export("mitx", {"id": mitx_dns_zone.id, "domain": mitx_dns_zone.name})
export("ocw", {"id": ocw_dns_zone.id, "domain": ocw_dns_zone.name})
export("ol", {"id": ol_dns_zone.id, "domain": ol_dns_zone.name})
