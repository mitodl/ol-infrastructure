"""Keycloak substructure definition."""

import json
import urllib.request
from functools import partial

import pulumi_keycloak as keycloak
from pulumi import Config, ResourceOptions

from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider
from ol_infrastructure.substructure.keycloak.ol_data_platform import (
    create_ol_data_platform_realm,
)
from ol_infrastructure.substructure.keycloak.ol_platform_engineering import (
    create_ol_platform_engineering_realm,
)
from ol_infrastructure.substructure.keycloak.olapps import create_olapps_realm

stack_info = parse_stack()
env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
keycloak_config = Config("keycloak")
keycloak_realm_config = Config("keycloak_realm")
setup_vault_provider()


def fetch_realm_public_key(keycloak_url: str, realm_id: str) -> str:
    """Fetch the public key for a given Keycloak realm."""
    with urllib.request.urlopen(f"{keycloak_url}/realms/{realm_id}/") as response:  # noqa: S310
        public_key_url_response = json.load(response)
    public_key = public_key_url_response["public_key"]
    if public_key:
        pem_lines = [
            "-----BEGIN PUBLIC KEY-----",
            public_key,
            "-----END PUBLIC KEY-----",
        ]
        cert_pem = "\n".join(pem_lines)
    else:
        cert_pem = "No public key found"
    return cert_pem


# Create a Keycloak provider cause we ran into an issue with pulumi reading
# config from stack definition.
keycloak_url = keycloak_config.require("url")
keycloak_provider = keycloak.Provider(
    "keycloak_provider",
    url=keycloak_url,
    realm="master",
    client_id=keycloak_config.get("client_id"),
    client_secret=keycloak_config.get("client_secret"),
    initial_login=True,
)

resource_options = ResourceOptions(provider=keycloak_provider)
mit_email_host = keycloak_realm_config.require("mit_email_host")
mit_email_password = keycloak_realm_config.require("mit_email_password")
mit_email_username = keycloak_realm_config.require("mit_email_username")
mailgun_email_host = keycloak_realm_config.require("mailgun_email_host")
mailgun_email_password = keycloak_realm_config.require("mailgun_email_password")
mailgun_email_username = keycloak_realm_config.require("mailgun_email_username")
mit_touchstone_cert = "MIIDCDCCAfCgAwIBAgIJAK/yS5ltGi7MMA0GCSqGSIb3DQEBBQUAMBYxFDASBgNVBAMTC2lkcC5taXQuZWR1MB4XDTEyMDczMDIxNTAxN1oXDTMyMDcyNTIxNTAxN1owFjEUMBIGA1UEAxMLaWRwLm1pdC5lZHUwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAwggEKAoIBAQDgC5Y2mM/VMThzTWrZ2uyv3Gw0mWU9NgQpWN1HQ/lLBxH1H6pMc5+fGpOdrvxH/Nepdg6uAJwZrclTDAHHpG/THb7K063NRtic8h9UYSqwxIWUCXI8qNijcWA2bW6PFEy4yIP611J+IzQxzD/ZiR+89ouzdjNBrPHzoaIoMwflftYnFc4L/qu4DxE/NWgANYPGEJfWUFTVpfNV1Iet60904zl+O7T79mwaQwwOMUWwk/DEQyvG6bf2uWL4aFx4laBOekrA+5rSHUXAFlhCreTnzZMkVoxSGqYlc5uZuZmpFCXZn+tNpsVYz+c4Hve3WOZwhx/7bMGCwlx7oovoQWQ5AgMBAAGjWTBXMDYGA1UdEQQvMC2CC2lkcC5taXQuZWR1hh5odHRwczovL2lkcC5taXQuZWR1L3NoaWJib2xldGgwHQYDVR0OBBYEFF5aINzhvMR+pOijYHtr3yCKsrMSMA0GCSqGSIb3DQEBBQUAA4IBAQDfVpscchXXa4Al/l9NGNwQ1shpQ8d+k+NpX2Q976jau9DhVHa42F8bfl1EeHLMFlN79aUxFZb3wvr0h5pq3a8F9aWHyKe+0R10ikVueDcAmg0V7MWthFdsyMwHPbnCdSXo2wh0GhjeIF3f3+hZZwrZ4sZqjX2RmsYnyXgS1r5mzuu4W447Q1fbC5BeZTefUhJcfHQ56ztIFtLJdRuHHnqj09CaQVMD1FtovM86vYwVMwMsgOgkN3c7tW6kXHHBHeEA31xUJsqXGTRlwMSyJTju3SFvhXI/8ZIxshTzWURBo+vf6A6QQvSvJAju4zVLZy83YB/cvAFsV3BexZ4xzuQD"  # pragma: allowlist secret # noqa: E501
session_secret = keycloak_realm_config.require("session_secret")

fetch_realm_public_key_partial = partial(
    fetch_realm_public_key,
    keycloak_url,
)

create_ol_platform_engineering_realm(
    keycloak_provider,
    keycloak_url,
    env_name,
    mit_email_password,
    mit_email_username,
    mit_email_host,
    session_secret,
    fetch_realm_public_key_partial,
)
create_ol_data_platform_realm(
    keycloak_provider,
    keycloak_url,
    env_name,
    stack_info,
    mit_email_password,
    mit_email_username,
    mit_email_host,
    mit_touchstone_cert,
    session_secret,
    fetch_realm_public_key_partial,
)
create_olapps_realm(
    keycloak_provider,
    keycloak_url,
    env_name,
    stack_info,
    mailgun_email_password,
    mailgun_email_username,
    mailgun_email_host,
    mit_touchstone_cert,
    session_secret,
    fetch_realm_public_key_partial,
)
