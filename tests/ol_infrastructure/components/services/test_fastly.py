"""Tests for the resources OLFastlyTLS declares.

The component adopts subscriptions and DNS records that already serve
production traffic, so the tests pin the resource names it is given, the alias
that keeps Pulumi from replacing them, and the DNS targets it picks.
"""

from __future__ import annotations

import asyncio

import pulumi

# Python 3.14+ compatibility
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

GET_TLS_CONFIGURATION = "fastly:index/getTlsConfiguration:getTlsConfiguration"
TLS_DNS_RECORDS = [
    {"recordType": "A", "recordValue": "151.101.2.133", "region": "global"},
    {"recordType": "A", "recordValue": "151.101.66.133", "region": "global"},
    {"recordType": "A", "recordValue": "199.232.1.1", "region": "na/eu"},
    {
        "recordType": "CNAME",
        "recordValue": "j.sni.global.fastly.net",
        "region": "global",
    },
]


class FastlyMocks(pulumi.runtime.Mocks):
    def __init__(self) -> None:
        self.resources: dict[str, pulumi.runtime.MockResourceArgs] = {}
        self.calls: list[pulumi.runtime.MockCallArgs] = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources[args.name] = args
        outputs = dict(args.inputs)
        if args.typ == "fastly:index/tlsSubscription:TlsSubscription":
            outputs["managedDnsChallenges"] = []
        return [args.name + "_id", outputs]

    def call(self, args: pulumi.runtime.MockCallArgs):
        self.calls.append(args)
        if args.token == GET_TLS_CONFIGURATION:
            return {"id": "tls-config-id", "dnsRecords": TLS_DNS_RECORDS}
        return {}


mocks = FastlyMocks()
pulumi.runtime.set_mocks(mocks)

import pytest  # noqa: E402

from ol_infrastructure.components.services.fastly import (  # noqa: E402
    OLFastlyDNSRecordConfig,
    OLFastlyTLS,
    OLFastlyTLSConfig,
)


@pytest.fixture(autouse=True, scope="module")
def _fastly_mocks():
    """Every test module installs its own mocks at import, and the last one
    collected wins, so reinstall these before the tests here run.
    """
    pulumi.runtime.set_mocks(mocks)


def _tls(name: str, **overrides) -> OLFastlyTLS:
    config = OLFastlyTLSConfig(
        subscription_resource_name=f"{name}-subscription",
        validation_resource_name=f"{name}-validation",
        domains=[f"{name}.example.com"],
        **overrides,
    )
    return OLFastlyTLS(name, tls_config=config)


@pulumi.runtime.test
def test_subscription_defaults():
    tls = _tls("defaults")

    def check(args):
        authority, configuration_id, domains, common_name, force_update = args
        assert authority == "certainly"
        assert configuration_id == "tls-config-id"
        assert domains == ["defaults.example.com"]
        assert common_name is None
        assert force_update is None

    return pulumi.Output.all(
        tls.subscription.certificate_authority,
        tls.subscription.configuration_id,
        tls.subscription.domains,
        tls.subscription.common_name,
        tls.subscription.force_update,
    ).apply(check)


@pulumi.runtime.test
def test_tls_configuration_is_looked_up_by_name_and_protocols():
    _tls(
        "protocols",
        tls_configuration_name="TLS v1.3+0RTT",
        tls_protocols=["1.2", "1.3+0RTT"],
    )
    lookup = next(
        call for call in reversed(mocks.calls) if call.token == GET_TLS_CONFIGURATION
    )
    assert lookup.args["name"] == "TLS v1.3+0RTT"
    assert lookup.args["tlsProtocols"] == ["1.2", "1.3+0RTT"]
    assert lookup.args["default"] is False


@pulumi.runtime.test
def test_resources_keep_the_names_they_are_given():
    tls = _tls("named")

    def check(_):
        assert "named-subscription" in mocks.resources
        assert "named-validation" in mocks.resources

    return tls.validation.subscription_id.apply(check)


@pulumi.runtime.test
def test_validation_waits_on_the_subscription():
    tls = _tls("validated")

    def check(subscription_id):
        assert subscription_id == "validated-subscription_id"

    return tls.validation.subscription_id.apply(check)


def test_children_alias_their_former_top_level_urn():
    """A subscription that predates the component must not be replaced."""
    tls = _tls("aliased")
    for resource in (tls.subscription, tls.validation):
        assert resource._aliases


@pulumi.runtime.test
def test_apex_record_takes_every_a_record():
    tls = _tls(
        "apex",
        dns_records=[
            OLFastlyDNSRecordConfig(
                resource_name="apex-dns", domain="example.com", zone_id="Z1"
            )
        ],
    )

    def check(args):
        record_type, records, zone_id, ttl, allow_overwrite = args
        assert record_type == "A"
        assert records == ["151.101.2.133", "151.101.66.133", "199.232.1.1"]
        assert zone_id == "Z1"
        assert ttl == 300
        assert allow_overwrite is True

    record = tls.dns_records[0]
    return pulumi.Output.all(
        record.type, record.records, record.zone_id, record.ttl, record.allow_overwrite
    ).apply(check)


@pulumi.runtime.test
def test_region_narrows_the_targets():
    tls = _tls(
        "regional",
        dns_records=[
            OLFastlyDNSRecordConfig(
                resource_name="regional-dns",
                domain="example.com",
                zone_id="Z1",
                region="global",
            )
        ],
    )

    def check(records):
        assert records == ["151.101.2.133", "151.101.66.133"]

    return tls.dns_records[0].records.apply(check)


@pulumi.runtime.test
def test_subdomain_record_is_a_cname():
    tls = _tls(
        "subdomain",
        dns_records=[
            OLFastlyDNSRecordConfig(
                resource_name="subdomain-dns",
                domain="www.example.com",
                zone_id="Z1",
                record_type="CNAME",
            )
        ],
    )

    def check(args):
        record_type, records = args
        assert record_type == "CNAME"
        assert records == ["j.sni.global.fastly.net"]

    record = tls.dns_records[0]
    return pulumi.Output.all(record.type, record.records).apply(check)


def test_no_dns_records_by_default():
    assert _tls("no-dns").dns_records == []
