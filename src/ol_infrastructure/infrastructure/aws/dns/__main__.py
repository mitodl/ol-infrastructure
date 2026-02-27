import json

import pulumi
import pulumi_aws as aws
from pulumi import export
from pulumi_aws import kms, route53

from ol_infrastructure.lib.aws.route53_helper import zone_opts
from ol_infrastructure.lib.ol_types import AWSBase, BusinessUnit, Environment


def enable_zone_dnssec(
    zone_slug: str,
    zone: route53.Zone,
    kms_key_arn: pulumi.Output[str],
) -> None:
    """Create a KeySigningKey and enable DNSSEC signing for a hosted zone."""
    ksk = route53.KeySigningKey(
        f"{zone_slug}_ksk",
        hosted_zone_id=zone.id,
        key_management_service_arn=kms_key_arn,
        name="dnssec_ksk",
        status="ACTIVE",
        opts=pulumi.ResourceOptions(depends_on=[zone]),
    )
    route53.HostedZoneDnsSec(
        f"{zone_slug}_dnssec",
        hosted_zone_id=zone.id,
        opts=pulumi.ResourceOptions(depends_on=[ksk]),
    )


# Shared KMS key for Route53 DNSSEC (must be ECC_NIST_P256 in us-east-1)
caller = aws.get_caller_identity()
dnssec_kms_key = kms.Key(
    "route53_dnssec_kms_key",
    description="Shared KMS key for Route53 DNSSEC signing across all hosted zones",
    customer_master_key_spec="ECC_NIST_P256",
    key_usage="SIGN_VERIFY",
    deletion_window_in_days=7,
    enable_key_rotation=False,
    policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "EnableIAMRootPermissions",
                    "Effect": "Allow",
                    "Principal": {"AWS": f"arn:aws:iam::{caller.account_id}:root"},
                    "Action": "kms:*",
                    "Resource": "*",
                },
                {
                    "Sid": "AllowRoute53DNSSECService",
                    "Effect": "Allow",
                    "Principal": {"Service": "dnssec-route53.amazonaws.com"},
                    "Action": [
                        "kms:DescribeKey",
                        "kms:GetPublicKey",
                        "kms:Sign",
                    ],
                    "Resource": "*",
                },
                {
                    "Sid": "AllowRoute53DNSSECServiceGrant",
                    "Effect": "Allow",
                    "Principal": {"Service": "dnssec-route53.amazonaws.com"},
                    "Action": "kms:CreateGrant",
                    "Resource": "*",
                    "Condition": {"Bool": {"kms:GrantIsForAWSResource": "true"}},
                },
            ],
        }
    ),
    tags=AWSBase(
        tags={
            "OU": BusinessUnit.operations,
            "Environment": Environment.operations,
        }
    ).tags,
)

kms.Alias(
    "route53_dnssec_kms_key_alias",
    name="alias/route53-dnssec",
    target_key_id=dnssec_kms_key.id,
)


# === Existing zones ===

mitxpro_legacy_dns_name = "mitxpro.mit.edu"
mitxpro_legacy_dns_zone = route53.Zone(
    "mitxpro_legacy_subdomain",
    name=mitxpro_legacy_dns_name,
    comment="DNS Zone for legacy xPro resources",
    tags=AWSBase(tags={"OU": "mitxpro", "Environment": "operations"}).tags,
    opts=zone_opts(mitxpro_legacy_dns_name),
)
enable_zone_dnssec("mitxpro_legacy", mitxpro_legacy_dns_zone, dnssec_kms_key.arn)

odl_dns_name = "odl.mit.edu"
odl_dns_zone = route53.Zone(
    "mitodl_subdomain",
    name=odl_dns_name,
    comment="DNS Zone used for ODL resources",
    tags=AWSBase(tags={"OU": "operations", "Environment": "operations"}).tags,
    opts=zone_opts(odl_dns_name),
)
enable_zone_dnssec("odl", odl_dns_zone, dnssec_kms_key.arn)

ol_dns_name = "ol.mit.edu"
ol_dns_zone = route53.Zone(
    "mitol_subdomain",
    name=ol_dns_name,
    comment="DNS Zone used for Open Learning resources",
    tags=AWSBase(tags={"OU": "operations", "Environment": "operations"}).tags,
    opts=zone_opts(ol_dns_name),
)
enable_zone_dnssec("ol", ol_dns_zone, dnssec_kms_key.arn)

mitx_dns_name = "mitx.mit.edu"
mitx_dns_zone = route53.Zone(
    "mitx_subdomain",
    name=mitx_dns_name,
    comment="DNS Zone used for MITx resources",
    tags=AWSBase(tags={"OU": "residential", "Environment": "mitx"}).tags,
    opts=zone_opts(mitx_dns_name),
)
enable_zone_dnssec("mitx", mitx_dns_zone, dnssec_kms_key.arn)

xpro_dns_name = "xpro.mit.edu"
xpro_dns_zone = route53.Zone(
    "xpro_subdomain",
    name=xpro_dns_name,
    comment="DNS Zone used for xPRO resources",
    tags=AWSBase(tags={"OU": "mitxpro", "Environment": "xpro"}).tags,
    opts=zone_opts(xpro_dns_name),
)
enable_zone_dnssec("xpro", xpro_dns_zone, dnssec_kms_key.arn)

mitxonline_dns_name = "mitxonline.mit.edu"
mitxonline_dns_zone = route53.Zone(
    "mitxonline_subdomain",
    name=mitxonline_dns_name,
    comment="DNS Zone used for MITx Online resources",
    tags=AWSBase(tags={"OU": "mitxonline", "Environment": "mitxonline"}).tags,
    opts=zone_opts(mitxonline_dns_name),
)
enable_zone_dnssec("mitxonline", mitxonline_dns_zone, dnssec_kms_key.arn)

ocw_dns_name = "ocw.mit.edu"
ocw_dns_zone = route53.Zone(
    "ocw_subdomain",
    name=ocw_dns_name,
    comment="DNS Zone used for OCW resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.ocw, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(ocw_dns_name),
)
enable_zone_dnssec("ocw", ocw_dns_zone, dnssec_kms_key.arn)

learn_dns_name = "learn.mit.edu"
learn_dns_zone = route53.Zone(
    "learn_subdomain",
    name=learn_dns_name,
    comment="DNS Zone used for mitopen resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(learn_dns_name),
)
enable_zone_dnssec("learn", learn_dns_zone, dnssec_kms_key.arn)

podcasts_dns_name = "podcasts.mit.edu"
podcasts_dns_zone = route53.Zone(
    "podcasts_subdomain",
    name=podcasts_dns_name,
    comment="DNS Zone used for mitopen resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(podcasts_dns_name),
)
enable_zone_dnssec("podcasts", podcasts_dns_zone, dnssec_kms_key.arn)

podcast_dns_name = "podcast.mit.edu"
podcast_dns_zone = route53.Zone(
    "podcast_subdomain",
    name=podcast_dns_name,
    comment="DNS Zone used for mitopen resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(podcast_dns_name),
)
enable_zone_dnssec("podcast", podcast_dns_zone, dnssec_kms_key.arn)


# === New zones ===
chalkradioblog_dns_name = "chalkradioblog.net"
chalkradioblog_dns_zone = route53.Zone(
    "chalkradioblog_net",
    name=chalkradioblog_dns_name,
    comment="DNS Zone for Chalk Radio blog",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.operations}
    ).tags,
    opts=zone_opts(chalkradioblog_dns_name),
)
enable_zone_dnssec("chalkradioblog", chalkradioblog_dns_zone, dnssec_kms_key.arn)

chalkradiopodcast_com_dns_name = "chalkradiopodcast.com"
chalkradiopodcast_com_dns_zone = route53.Zone(
    "chalkradiopodcast_com",
    name=chalkradiopodcast_com_dns_name,
    comment="DNS Zone for Chalk Radio podcast (.com)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.operations}
    ).tags,
    opts=zone_opts(chalkradiopodcast_com_dns_name),
)
enable_zone_dnssec(
    "chalkradiopodcast_com", chalkradiopodcast_com_dns_zone, dnssec_kms_key.arn
)

chalkradiopodcast_net_dns_name = "chalkradiopodcast.net"
chalkradiopodcast_net_dns_zone = route53.Zone(
    "chalkradiopodcast_net",
    name=chalkradiopodcast_net_dns_name,
    comment="DNS Zone for Chalk Radio podcast (.net)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.operations}
    ).tags,
    opts=zone_opts(chalkradiopodcast_net_dns_name),
)
enable_zone_dnssec(
    "chalkradiopodcast_net", chalkradiopodcast_net_dns_zone, dnssec_kms_key.arn
)

chalkradiopodcast_org_dns_name = "chalkradiopodcast.org"
chalkradiopodcast_org_dns_zone = route53.Zone(
    "chalkradiopodcast_org",
    name=chalkradiopodcast_org_dns_name,
    comment="DNS Zone for Chalk Radio podcast (.org)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.operations}
    ).tags,
    opts=zone_opts(chalkradiopodcast_org_dns_name),
)
enable_zone_dnssec(
    "chalkradiopodcast_org", chalkradiopodcast_org_dns_zone, dnssec_kms_key.arn
)

micromasters_dns_name = "micromasters.mit.edu"
micromasters_dns_zone = route53.Zone(
    "micromasters_subdomain",
    name=micromasters_dns_name,
    comment="DNS Zone for MicroMasters resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.micromasters, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(micromasters_dns_name),
)
enable_zone_dnssec("micromasters", micromasters_dns_zone, dnssec_kms_key.arn)

mitili_dns_name = "mitili.mit.edu"
mitili_dns_zone = route53.Zone(
    "mitili_subdomain",
    name=mitili_dns_name,
    comment="DNS Zone for MITILI resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.operations, "Environment": Environment.operations}
    ).tags,
    opts=zone_opts(mitili_dns_name),
)
enable_zone_dnssec("mitili", mitili_dns_zone, dnssec_kms_key.arn)

mitlearning_com_dns_name = "mitlearning.com"
mitlearning_com_dns_zone = route53.Zone(
    "mitlearning_com",
    name=mitlearning_com_dns_name,
    comment="DNS Zone for MIT Learning (.com)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(mitlearning_com_dns_name),
)
enable_zone_dnssec("mitlearning_com", mitlearning_com_dns_zone, dnssec_kms_key.arn)

mitlearning_net_dns_name = "mitlearning.net"
mitlearning_net_dns_zone = route53.Zone(
    "mitlearning_net",
    name=mitlearning_net_dns_name,
    comment="DNS Zone for MIT Learning (.net)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(mitlearning_net_dns_name),
)
enable_zone_dnssec("mitlearning_net", mitlearning_net_dns_zone, dnssec_kms_key.arn)

mitlearning_org_dns_name = "mitlearning.org"
mitlearning_org_dns_zone = route53.Zone(
    "mitlearning_org",
    name=mitlearning_org_dns_name,
    comment="DNS Zone for MIT Learning (.org)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(mitlearning_org_dns_name),
)
enable_zone_dnssec("mitlearning_org", mitlearning_org_dns_zone, dnssec_kms_key.arn)

mitopen_com_dns_name = "mitopen.com"
mitopen_com_dns_zone = route53.Zone(
    "mitopen_com",
    name=mitopen_com_dns_name,
    comment="DNS Zone for MIT Open (.com)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(mitopen_com_dns_name),
)
enable_zone_dnssec("mitopen_com", mitopen_com_dns_zone, dnssec_kms_key.arn)

mitopen_net_dns_name = "mitopen.net"
mitopen_net_dns_zone = route53.Zone(
    "mitopen_net",
    name=mitopen_net_dns_name,
    comment="DNS Zone for MIT Open (.net)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(mitopen_net_dns_name),
)
enable_zone_dnssec("mitopen_net", mitopen_net_dns_zone, dnssec_kms_key.arn)

mitopen_org_dns_name = "mitopen.org"
mitopen_org_dns_zone = route53.Zone(
    "mitopen_org",
    name=mitopen_org_dns_name,
    comment="DNS Zone for MIT Open (.org)",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(mitopen_org_dns_name),
)
enable_zone_dnssec("mitopen_org", mitopen_org_dns_zone, dnssec_kms_key.arn)

mitx_pro_dns_name = "mitx.pro"
mitx_pro_dns_zone = route53.Zone(
    "mitx_pro",
    name=mitx_pro_dns_name,
    comment="DNS Zone for MITx Pro (.pro TLD)",
    tags=AWSBase(tags={"OU": BusinessUnit.xpro, "Environment": Environment.xpro}).tags,
    opts=zone_opts(mitx_pro_dns_name),
)
enable_zone_dnssec("mitx_pro", mitx_pro_dns_zone, dnssec_kms_key.arn)

mitxpro_info_dns_name = "mitxpro.info"
mitxpro_info_dns_zone = route53.Zone(
    "mitxpro_info",
    name=mitxpro_info_dns_name,
    comment="DNS Zone for xPro (.info)",
    tags=AWSBase(tags={"OU": BusinessUnit.xpro, "Environment": Environment.xpro}).tags,
    opts=zone_opts(mitxpro_info_dns_name),
)
enable_zone_dnssec("mitxpro_info", mitxpro_info_dns_zone, dnssec_kms_key.arn)

mitxpro_net_dns_name = "mitxpro.net"
mitxpro_net_dns_zone = route53.Zone(
    "mitxpro_net",
    name=mitxpro_net_dns_name,
    comment="DNS Zone for xPro (.net)",
    tags=AWSBase(tags={"OU": BusinessUnit.xpro, "Environment": Environment.xpro}).tags,
    opts=zone_opts(mitxpro_net_dns_name),
)
enable_zone_dnssec("mitxpro_net", mitxpro_net_dns_zone, dnssec_kms_key.arn)

mitxpro_org_dns_name = "mitxpro.org"
mitxpro_org_dns_zone = route53.Zone(
    "mitxpro_org",
    name=mitxpro_org_dns_name,
    comment="DNS Zone for xPro (.org)",
    tags=AWSBase(tags={"OU": BusinessUnit.xpro, "Environment": Environment.xpro}).tags,
    opts=zone_opts(mitxpro_org_dns_name),
)
enable_zone_dnssec("mitxpro_org", mitxpro_org_dns_zone, dnssec_kms_key.arn)

open_dns_name = "open.mit.edu"
open_dns_zone = route53.Zone(
    "open_subdomain",
    name=open_dns_name,
    comment="DNS Zone for open.mit.edu resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.mit_open, "Environment": Environment.applications}
    ).tags,
    opts=zone_opts(open_dns_name),
)
enable_zone_dnssec("open", open_dns_zone, dnssec_kms_key.arn)

teach_dns_name = "teach.mit.edu"
teach_dns_zone = route53.Zone(
    "teach_subdomain",
    name=teach_dns_name,
    comment="DNS Zone for teach.mit.edu resources",
    tags=AWSBase(
        tags={"OU": BusinessUnit.operations, "Environment": Environment.operations}
    ).tags,
    opts=zone_opts(teach_dns_name),
)
enable_zone_dnssec("teach", teach_dns_zone, dnssec_kms_key.arn)


# === Exports ===

export("mitxpro_legacy_zone_id", mitxpro_legacy_dns_zone.id)
export("odl_zone_id", odl_dns_zone.id)
export("odl", {"id": odl_dns_zone.id, "domain": odl_dns_zone.name})
export("mitxonline", {"id": mitxonline_dns_zone.id, "domain": mitxonline_dns_zone.name})
export("xpro", {"id": xpro_dns_zone.id, "domain": xpro_dns_zone.name})
export("mitx", {"id": mitx_dns_zone.id, "domain": mitx_dns_zone.name})
export("ocw", {"id": ocw_dns_zone.id, "domain": ocw_dns_zone.name})
export("ol", {"id": ol_dns_zone.id, "domain": ol_dns_zone.name})
export("learn", {"id": learn_dns_zone.id, "domain": learn_dns_zone.name})
export("podcasts", {"id": podcasts_dns_zone.id, "domain": podcasts_dns_zone.name})
export("podcast", {"id": podcast_dns_zone.id, "domain": podcast_dns_zone.name})
export(
    "chalkradioblog",
    {"id": chalkradioblog_dns_zone.id, "domain": chalkradioblog_dns_zone.name},
)
export(
    "chalkradiopodcast_com",
    {
        "id": chalkradiopodcast_com_dns_zone.id,
        "domain": chalkradiopodcast_com_dns_zone.name,
    },
)
export(
    "chalkradiopodcast_net",
    {
        "id": chalkradiopodcast_net_dns_zone.id,
        "domain": chalkradiopodcast_net_dns_zone.name,
    },
)
export(
    "chalkradiopodcast_org",
    {
        "id": chalkradiopodcast_org_dns_zone.id,
        "domain": chalkradiopodcast_org_dns_zone.name,
    },
)
export(
    "micromasters",
    {"id": micromasters_dns_zone.id, "domain": micromasters_dns_zone.name},
)
export("mitili", {"id": mitili_dns_zone.id, "domain": mitili_dns_zone.name})
export(
    "mitlearning_com",
    {"id": mitlearning_com_dns_zone.id, "domain": mitlearning_com_dns_zone.name},
)
export(
    "mitlearning_net",
    {"id": mitlearning_net_dns_zone.id, "domain": mitlearning_net_dns_zone.name},
)
export(
    "mitlearning_org",
    {"id": mitlearning_org_dns_zone.id, "domain": mitlearning_org_dns_zone.name},
)
export(
    "mitopen_com",
    {"id": mitopen_com_dns_zone.id, "domain": mitopen_com_dns_zone.name},
)
export(
    "mitopen_net",
    {"id": mitopen_net_dns_zone.id, "domain": mitopen_net_dns_zone.name},
)
export(
    "mitopen_org",
    {"id": mitopen_org_dns_zone.id, "domain": mitopen_org_dns_zone.name},
)
export("mitx_pro", {"id": mitx_pro_dns_zone.id, "domain": mitx_pro_dns_zone.name})
export(
    "mitxpro_info",
    {"id": mitxpro_info_dns_zone.id, "domain": mitxpro_info_dns_zone.name},
)
export(
    "mitxpro_net",
    {"id": mitxpro_net_dns_zone.id, "domain": mitxpro_net_dns_zone.name},
)
export(
    "mitxpro_org",
    {"id": mitxpro_org_dns_zone.id, "domain": mitxpro_org_dns_zone.name},
)
export("open", {"id": open_dns_zone.id, "domain": open_dns_zone.name})
export("teach", {"id": teach_dns_zone.id, "domain": teach_dns_zone.name})
