import pulumi_mailgun as mailgun
from pulumi import Config
from pulumi_aws import route53

from ol_infrastructure.lib.aws.route53_helper import (
    lookup_zone_id_from_domain,
    route53_record_opts,
)
from ol_infrastructure.lib.mailgun_helper import (
    mailgun_credential_opts,
    mailgun_domain_opts,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()
mailgun_config = Config("mailgun")
api_key = mailgun_config.require("apiKey")
domains = mailgun_config.require_object("domains")

# DMARC policy record value - uniform across all Mailgun-managed domains
DMARC_RECORD_VALUE = (
    "v=DMARC1; p=none; pct=100; fo=1; ri=3600;"
    " rua=mailto:e00d0fc6@dmarc.mailgun.org,mailto:b0bb505c@inbox.ondmarc.com;"
    " ruf=mailto:e00d0fc6@dmarc.mailgun.org,mailto:b0bb505c@inbox.ondmarc.com;"
)

thirty_minutes = 60 * 30

for domain_cfg in domains:
    name: str = domain_cfg["name"]
    managed: bool = domain_cfg.get("managed", False)

    zone_id = lookup_zone_id_from_domain(name)
    if zone_id is None:
        msg = f"No Route53 hosted zone found for domain '{name}'"
        raise ValueError(msg)

    # --- Mailgun domain ---
    mg_domain = mailgun.Domain(
        f"ol-mailgun-domain-{name}",
        click_tracking=domain_cfg.get("click_tracking", True),
        dkim_key_size=domain_cfg.get("dkim_key_size", 2048),
        dkim_selector=domain_cfg.get("dkim_selector", "mx"),
        force_dkim_authority=domain_cfg.get("force_dkim_authority", False),
        name=name,
        open_tracking=domain_cfg.get("open_tracking", True),
        region=domain_cfg.get("region", "us"),
        smtp_password=domain_cfg["smtp_password"],
        spam_action=domain_cfg.get("spam_action", "disabled"),
        use_automatic_sender_security=domain_cfg.get(
            "use_automatic_sender_security", True
        ),
        web_scheme=domain_cfg.get("web_scheme", "https"),
        wildcard=domain_cfg.get("wildcard", False),
        opts=mailgun_domain_opts(name, api_key, managed=managed),
    )

    # --- SMTP credentials (one or more per domain) ---
    for cred in domain_cfg.get("credentials", []):
        login: str = cred["login"]
        mailgun.DomainCredential(
            f"ol-mailgun-credential-{login}-{name}",
            domain=mg_domain.id,
            login=login,
            password=cred["smtp_password"],
            opts=mailgun_credential_opts(name, login, api_key, managed=managed),
        )

    # --- Route53 records ---
    route53.Record(
        f"ol-mailgun-spf-{name}",
        name=name,
        type="TXT",
        ttl=thirty_minutes,
        records=["v=spf1 include:mailgun.org ~all"],
        zone_id=zone_id,
        opts=route53_record_opts(zone_id, name, "TXT", managed=managed),
    )

    # Mailgun sets the DKIM value; we read it back from the domain resource.
    # The provider does not support uploading a custom DKIM value despite the UI
    # offering that option.
    route53.Record(
        f"ol-mailgun-dkim-{name}",
        name=f"mx._domainkey.{name}",
        type="TXT",
        ttl=thirty_minutes,
        records=[
            mg_domain.sending_records_sets[0].value.apply(
                lambda value: f'{value[:254]}""{value[254:]}'
            )
        ],
        zone_id=zone_id,
        opts=route53_record_opts(
            zone_id, f"mx._domainkey.{name}", "TXT", managed=managed
        ),
    )

    route53.Record(
        f"ol-mailgun-mx-{name}",
        name=name,
        type="MX",
        ttl=thirty_minutes,
        records=["10 mxa.mailgun.org", "10 mxb.mailgun.org"],
        zone_id=zone_id,
        opts=route53_record_opts(zone_id, name, "MX", managed=managed),
    )

    route53.Record(
        f"ol-mailgun-tracking-{name}",
        name=f"email.{name}",
        type="CNAME",
        ttl=thirty_minutes,
        records=["mailgun.org"],
        zone_id=zone_id,
        opts=route53_record_opts(zone_id, f"email.{name}", "CNAME", managed=managed),
    )

    route53.Record(
        f"ol-mailgun-dmarc-{name}",
        name=f"_dmarc.{name}",
        type="TXT",
        ttl=thirty_minutes,
        records=[DMARC_RECORD_VALUE],
        zone_id=zone_id,
        opts=route53_record_opts(zone_id, f"_dmarc.{name}", "TXT", managed=managed),
    )
