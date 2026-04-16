import httpx
import pulumi

MAILGUN_API_BASE = "https://api.mailgun.net"
HTTP_OK = 200


def mailgun_domain_opts(
    domain_name: str,
    api_key: str,
    *,
    managed: bool,
) -> pulumi.ResourceOptions:
    """Return ResourceOptions for a Mailgun domain resource.

    If managed is True, the domain is already in Pulumi state and no import is needed.
    If managed is False, the Mailgun API is queried. If the domain exists, import opts
    are returned so Pulumi brings it into state on the next up. If the domain does not
    exist, empty opts are returned so Pulumi creates it.

    After a successful ``pulumi up`` that imports the domain, set ``managed: true`` in
    the stack config so subsequent runs skip the API call.

    :param domain_name: The Mailgun domain name, e.g. ``mail.learn.mit.edu``
    :param api_key: Mailgun API key (US region)
    :param managed: Whether the domain is already under Pulumi management

    :returns: ResourceOptions with import_ set if the domain exists and is not yet
        managed
    :rtype: pulumi.ResourceOptions
    """
    if managed:
        return pulumi.ResourceOptions()

    resp = httpx.get(
        f"{MAILGUN_API_BASE}/v4/domains/{domain_name}",
        auth=("api", api_key),
    )
    if resp.status_code == HTTP_OK:
        return pulumi.ResourceOptions(import_=f"us:{domain_name}")
    return pulumi.ResourceOptions()


def mailgun_credential_opts(
    domain_name: str,
    login: str,
    api_key: str,
    *,
    managed: bool,
) -> pulumi.ResourceOptions:
    """Return ResourceOptions for a Mailgun DomainCredential resource.

    Mailgun's API does not expose SMTP passwords, so ``ignore_changes=["password"]``
    is always included in the returned opts. If managed is False, the credential list
    for the domain is fetched and import opts are returned if the login already exists.

    After a successful ``pulumi up`` that imports the credential, set ``managed: true``
    in the stack config so subsequent runs skip the API call.

    :param domain_name: The Mailgun domain name, e.g. ``mail.learn.mit.edu``
    :param login: The credential login without the domain suffix, e.g. ``no-reply``
    :param api_key: Mailgun API key (US region)
    :param managed: Whether the credential is already under Pulumi management

    :returns: ResourceOptions with import_ set if applicable; always includes
        ignore_changes=["password"]
    :rtype: pulumi.ResourceOptions
    """
    if managed:
        return pulumi.ResourceOptions(ignore_changes=["password"])

    resp = httpx.get(
        f"{MAILGUN_API_BASE}/v3/{domain_name}/credentials",
        auth=("api", api_key),
    )
    if resp.status_code == HTTP_OK:
        logins = {item["login"] for item in resp.json().get("items", [])}
        if f"{login}@{domain_name}" in logins:
            return pulumi.ResourceOptions(
                import_=f"us:{login}@{domain_name}",
                ignore_changes=["password"],
            )
    return pulumi.ResourceOptions(ignore_changes=["password"])
