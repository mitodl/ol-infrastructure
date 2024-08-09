import pulumi
import pulumi_mailgun as mailgun
from pulumi import Config, ResourceOptions, StackReference
from pulumi_aws import route53

from ol_infrastructure.lib.pulumi_helper import parse_stack

env_config = pulumi.Config("environment")
stack_info = parse_stack()
mitlearn_config = Config("mitlearn")
dns_stack = StackReference("infrastructure.aws.dns")
mitlearn_zone_id = dns_stack.get_output("learn")["id"]

ol_mailgun_mitlearn_domain = mailgun.Domain(
    f"ol-mailgun-mitlearn-domain-{stack_info.env_suffix}",
    click_tracking=True,
    dkim_key_size=2048,
    dkim_selector=mitlearn_config.require("dkim_public_key"),
    force_dkim_authority=False,
    name=mitlearn_config.require("url"),
    open_tracking=True,
    smtp_password=mitlearn_config.require("smtp_postmaster_password"),
    web_scheme="https",
    wildcard=False,
)

domain_credential_resource = mailgun.DomainCredential(
    "domainCredentialResource",
    domain=ol_mailgun_mitlearn_domain.id,
    login="no-reply",
    password=mitlearn_config.require("smtp_noreply_password"),
)

thirty_minutes = 60 * 30
# Mailgun MITLearn SPF record
route53.Record(
    "ol-mailgun-mitlearn-spf-record",
    name=mitlearn_config.require("url"),
    type="TXT",
    ttl=thirty_minutes,
    records=["v=spf1 include:mailgun.org ~all"],
    zone_id=mitlearn_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)

# Mailgun MITLearn DKIM record
route53.Record(
    "ol-mailgun-mitlearn-dkim-record",
    name=f"mx._domainkey.{mitlearn_config.require("url")}",
    type="TXT",
    ttl=thirty_minutes,
    records=[f"v=DKIM1; k=rsa; p={mitlearn_config.require("dkim_public_key")}"],
    zone_id=mitlearn_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)

# Mailgun MITLearn MX record
route53.Record(
    "ol-mailgun-mitlearn-mx-record",
    name=mitlearn_config.require("url"),
    type="MX",
    ttl=thirty_minutes,
    records=["mxa.mailgun.org", "mxb.mailgun.org"],
    zone_id=mitlearn_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)

# Mailgun MITLearn Tracking record
route53.Record(
    "ol-mailgun-mitlearn-tracking-record",
    name=f"email.{mitlearn_config.require("url")}",
    type="CNAME",
    ttl=thirty_minutes,
    records=["mailgun.org"],
    zone_id=mitlearn_zone_id,
    opts=ResourceOptions(delete_before_replace=True),
)
