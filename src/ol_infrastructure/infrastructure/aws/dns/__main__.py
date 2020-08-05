from pulumi import export, ResourceOptions
from pulumi_aws import route53
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.aws.route53_helper import zone_opts

mitxpro_legacy_dns_name = 'mitxpro.mit.edu'
mitxpro_legacy_opts = zone_opts(mitxpro_legacy_dns_name)
mitxpro_legacy_dns_zone = route53.Zone(
    'mitxpro_legacy_subdomain',
    name=mitxpro_legacy_dns_name,
    comment='DNS Zone for legacy xPro resources',
    tags=AWSBase(tags={'OU': 'mitxpro', 'Environment': 'operations'}).tags,
    opts=mitxpro_legacy_opts
)

odl_dns_name = 'odl.mit.edu'
odl_opts = zone_opts(odl_dns_name)
odl_dns_zone = route53.Zone(
    'mitodl_subdomain',
    name=odl_dns_name,
    comment='DNS Zone used for ODL resources',
    tags=AWSBase(tags={'OU': 'operations', 'Environment': 'operations'}).tags,
    opts=odl_opts
)

export('mitxpro_legacy_zone_id', mitxpro_legacy_dns_zone.id)
export('odl_zone_id', odl_dns_zone.id)
