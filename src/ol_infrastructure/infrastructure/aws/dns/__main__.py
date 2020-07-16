from pulumi import export, ResourceOptions
from pulumi_aws import route53
from ol_infrastructure.lib.ol_types import AWSBase

mitxpro_dns_zone = route53.Zone(
    'mitxpro_legacy_subdomain',
    name='mitxpro.mit.edu',
    comment='DNS Zone for legacy xPro resources',
    tags=AWSBase(tags={'OU': 'mitxpro', 'Environment': 'operations'}).tags,
)

export('mitxpro_zone_id', mitxpro_dns_zone.id)
