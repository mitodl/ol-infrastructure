"""Reusable Fastly resources."""

from typing import Literal

import pulumi
import pulumi_fastly as fastly
from pulumi_aws import route53
from pydantic import BaseModel, ConfigDict

from bridge.lib.magic_numbers import FIVE_MINUTES
from ol_infrastructure.lib.aws.route53_helper import (
    fastly_certificate_validation_records,
)


class OLFastlyDNSRecordConfig(BaseModel):
    """A Route53 record that points a domain at Fastly."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    resource_name: str
    domain: str
    zone_id: str | pulumi.Output[str]
    # A zone apex cannot be a CNAME, so it takes the anycast A records.
    record_type: Literal["A", "CNAME"] = "A"
    # Restrict the targets to one Fastly region, e.g. "global". None takes every
    # record of the type that the TLS configuration offers.
    region: str | None = None


class OLFastlyTLSConfig(BaseModel):
    """Configuration for a Fastly-managed TLS certificate."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Pulumi resource names are explicit so that an existing subscription can
    # be adopted without being replaced.
    subscription_resource_name: str
    validation_resource_name: str
    domains: list[str] | pulumi.Output[list[str]]
    # Must be one of `domains`. Fastly uses the first domain when it is unset.
    common_name: str | None = None
    certificate_authority: Literal["certainly", "lets-encrypt", "globalsign"] = (
        "certainly"
    )
    # Selects one of the configurations listed at
    # https://manage.fastly.com/network/tls-configurations
    tls_configuration_name: str = "TLS v1.3"
    tls_protocols: list[str] = ["1.2", "1.3"]
    # Lets the subscription be changed while its domains serve traffic.
    force_update: bool | None = None
    dns_records: list[OLFastlyDNSRecordConfig] = []


class OLFastlyTLS(pulumi.ComponentResource):
    """A Fastly TLS subscription, its DNS validation, and the DNS records that
    send traffic for its domains to Fastly.
    """

    def __init__(
        self,
        name: str,
        tls_config: OLFastlyTLSConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        """Create the subscription and the records that validate and serve it.

        :param name: The Pulumi name of the component.
        :param tls_config: The certificate and DNS settings.
        :param opts: Resource options. Must carry the Fastly provider.
        """
        super().__init__(
            "ol:infrastructure.services.fastly:OLFastlyTLS",
            name,
            None,
            opts,
        )

        # These resources were declared at the top level of each stack before
        # this component existed. Without the alias Pulumi would replace live
        # certificates and DNS records.
        resource_options = pulumi.ResourceOptions(
            parent=self,
            aliases=[pulumi.Alias(parent=pulumi.ROOT_STACK_RESOURCE)],
        )

        self.tls_configuration = fastly.get_tls_configuration(
            default=False,
            name=tls_config.tls_configuration_name,
            tls_protocols=tls_config.tls_protocols,
            opts=pulumi.InvokeOptions(parent=self),
        )

        self.subscription = fastly.TlsSubscription(
            tls_config.subscription_resource_name,
            certificate_authority=tls_config.certificate_authority,
            common_name=tls_config.common_name,
            domains=tls_config.domains,
            configuration_id=self.tls_configuration.id,
            force_update=tls_config.force_update,
            opts=resource_options,
        )

        # Left unparented. The challenge records are created inside an apply, so
        # a preview cannot show whether an alias matched, and a mismatch would
        # delete the record that the certificate renews against.
        self.subscription.managed_dns_challenges.apply(
            fastly_certificate_validation_records
        )

        self.validation = fastly.TlsSubscriptionValidation(
            tls_config.validation_resource_name,
            subscription_id=self.subscription.id,
            opts=resource_options,
        )

        self.dns_records = [
            route53.Record(
                record.resource_name,
                name=record.domain,
                type=record.record_type,
                ttl=FIVE_MINUTES,
                records=self.dns_record_values(record.record_type, record.region),
                zone_id=record.zone_id,
                allow_overwrite=True,
                opts=resource_options,
            )
            for record in tls_config.dns_records
        ]

        self.register_outputs({})

    def dns_record_values(
        self, record_type: Literal["A", "CNAME"], region: str | None = None
    ) -> list[str]:
        """List the addresses that a domain on this certificate must resolve to.

        :param record_type: The DNS record type to return targets for.
        :param region: Only return targets in this Fastly region.
        :returns: The record values for the TLS configuration in use.
        :rtype: list[str]
        """
        return [
            record.record_value
            for record in self.tls_configuration.dns_records
            if record.record_type == record_type
            and (region is None or record.region == region)
        ]
