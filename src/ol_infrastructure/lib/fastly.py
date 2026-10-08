import re
import textwrap
from pathlib import Path

import pulumi
import pulumi_fastly as fastly

from bridge.secrets.sops import read_yaml_secrets

# Snippet-only. Fastly validates conditions and request settings more loosely and we
# have live ones (with '/' and ',') that this pattern would reject.
VCL_SNIPPET_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_\-. ]*$")


def validate_vcl_snippet_name(name: str) -> str:
    """Raise unless the name is accepted by Fastly's VCL snippet endpoint."""
    if not VCL_SNIPPET_NAME_PATTERN.fullmatch(name):
        msg = (
            f"Invalid Fastly VCL snippet name {name!r}. Name must start with a letter "
            "and contain only alphanumeric, underscore, hyphen, period, and space "
            "characters."
        )
        raise ValueError(msg)
    return name


def vcl_snippet(
    *,
    name: str,
    content: str,
    type: str,  # noqa: A002
    priority: int | None = None,
) -> fastly.ServiceVclSnippetArgs:
    """Build snippet args, rejecting an illegal name before the resource registers.

    Raising here aborts the program before the ServiceVcl is registered, so the
    name never reaches the Fastly API. Otherwise it 400s mid-apply, after Pulumi
    has cloned a service version, and the failed run persists the uncreated
    snippet to state where the provider's SetDiff reads it back as Unmodified --
    no later `up` can heal it. The k8s_apps pipelines have no preview job, so the
    pre-merge guard is the call-site scan in tests/, not this.
    """
    return fastly.ServiceVclSnippetArgs(
        name=validate_vcl_snippet_name(name),
        content=content,
        type=type,
        priority=priority,
    )


# The SHA256 hash of an empty string -- a fixed, public constant (not a secret),
# used as the payload hash for unsigned-body GET requests in AWS SigV4 signing.
_SHA256_EMPTY_STRING = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # pragma: allowlist secret


def fastly_backend_identifier(backend_name: str) -> str:
    """Return the VCL identifier (`F_...`) Fastly generates for a backend name."""
    return "F_" + re.sub(r"[^A-Za-z0-9_]", "_", backend_name)


def s3_sigv4_signing_vcl(
    access_key_id: str,
    secret_access_key: str,
    bucket_host: str,
    backend_name: str,
) -> str:
    """Build VCL that signs requests to one S3 backend with AWS SigV4.

    Install the result as a `miss` snippet and again as a `pass` snippet. It is
    wrapped in a `req.backend` guard so the signature (and the key it embeds) is
    only ever applied to requests bound for `backend_name`.

    Four things learned the hard way on the mit-learn pilot, kept in the body:
    - `%0A` is Fastly's hex escape for a newline in a double-quoted string; `\\n`
      is a literal backslash and `n`.
    - There are deliberately no VCL `#` comments in the body: one containing a
      double quote breaks Fastly's parser.
    - `bereq.http.host` is set explicitly. The backend's `override_host` is not
      reflected in it yet when a `miss` snippet runs, so signing whatever it held
      produced SignatureDoesNotMatch.
    - The canonical query string is hardcoded empty, so the query string is
      stripped from the backend request before signing.

    The canonical URI is the S3-style percent-encoded path, and that same encoded
    path is what is sent to S3, so what is signed always equals what is requested.
    Browsers send characters like `( ) [ ] @ + ! *` literally but S3 canonicalizes
    them as `%XX`, so signing the raw path gets a 403 for any key containing them.
    The encoding is `urlencode(urldecode(path))`, with three details (measured in
    a Fastly Fiddle, since the function docs don't say):
    - `urlencode` leaves exactly A-Za-z0-9 - _ . ~ alone and uses uppercase hex,
      which is the set S3 expects. Decoding first stops `%28` becoming `%2528`.
    - `urldecode` turns `+` into a space, so a literal `+` is shielded as `%2B`
      first. `%252B` and `%252[Ff]` are used because `%25` is the double-quoted
      string escape for a literal `%`, and a bare `%2B` or `%2[` is read as a hex
      escape (`%2[` is a lint error).
    - `urlencode` also encodes `/`, so the separators are restored afterwards.
    """
    body = textwrap.dedent(
        f"""\
        declare local var.aws_access_key_id STRING;
        declare local var.aws_secret_access_key STRING;
        declare local var.date_stamp STRING;
        declare local var.amz_date STRING;
        declare local var.payload_hash STRING;
        declare local var.canonical_uri STRING;
        declare local var.canonical_headers STRING;
        declare local var.signed_headers STRING;
        declare local var.canonical_request STRING;
        declare local var.hashed_canonical_request STRING;
        declare local var.credential_scope STRING;
        declare local var.string_to_sign STRING;
        declare local var.signature STRING;

        set var.aws_access_key_id = "{access_key_id}";
        set var.aws_secret_access_key = "{secret_access_key}";

        set bereq.url = querystring.remove(bereq.url);
        set var.canonical_uri = regsuball(bereq.url.path, "\\+", "%252B");
        set var.canonical_uri = urlencode(urldecode(var.canonical_uri));
        set var.canonical_uri = regsuball(var.canonical_uri, "%252[Ff]", "/");
        set bereq.url = var.canonical_uri;

        set var.date_stamp = strftime({{"%Y%m%d"}}, now);
        set var.amz_date = strftime({{"%Y%m%dT%H%M%SZ"}}, now);
        set var.payload_hash = "{_SHA256_EMPTY_STRING}";

        set bereq.http.host = "{bucket_host}";
        unset bereq.http.Authorization;
        set bereq.http.x-amz-date = var.amz_date;
        set bereq.http.x-amz-content-sha256 = var.payload_hash;

        set var.signed_headers = "host;x-amz-content-sha256;x-amz-date";
        set var.canonical_headers = "host:" + bereq.http.host + "%0A" + "x-amz-content-sha256:" + var.payload_hash + "%0A" + "x-amz-date:" + var.amz_date + "%0A";

        set var.canonical_request = "GET" + "%0A" + var.canonical_uri + "%0A" + "" + "%0A" + var.canonical_headers + "%0A" + var.signed_headers + "%0A" + var.payload_hash;
        set var.hashed_canonical_request = digest.hash_sha256(var.canonical_request);

        set var.credential_scope = var.date_stamp + "/us-east-1/s3/aws4_request";
        set var.string_to_sign = "AWS4-HMAC-SHA256" + "%0A" + var.amz_date + "%0A" + var.credential_scope + "%0A" + var.hashed_canonical_request;

        set var.signature = digest.awsv4_hmac(var.aws_secret_access_key, var.date_stamp, "us-east-1", "s3", var.string_to_sign);

        set bereq.http.Authorization = "AWS4-HMAC-SHA256 Credential=" + var.aws_access_key_id + "/" + var.credential_scope + ", SignedHeaders=" + var.signed_headers + ", Signature=" + var.signature;
        """
    )
    guard = fastly_backend_identifier(backend_name)
    return f"if (req.backend == {guard}) {{\n" + textwrap.indent(body, "  ") + "}"


# Documentation:
# https://docs.fastly.com/en/guides/custom-log-formats#version-2-log-format
# client_ip : The 'true' client IP address.
# client_bot_name : name of the bot making the request if applicable
# client_browser_name : name of the web browser making the request if applicable
# client_browser_version : version number of the web broswer making the request if applicable
# client_class_* : true/false representing facts regarding the client
# client_platform_* : true/false representing facts about the device the client is using
# fastly_is_edge : True if no other fastly servers have seen this request, false otherwise.
# fastly_server : The fastly server that handled this request. May or may not include 3 letter POP
# fastly_pop_identifier : three letter POP identifer for the server handling this request
# fastly_background_fetch : Whether VCL is being evaluated for a stale while revalidate request to a backend.
# geo_city : city or town name the request originated from, lowercase
# geo_conn_speed : broadband, cable, dialup, mobile, oc12, oc3, t1, t3, satellite, wireless, xdsl
# geo_conn_type : wired, wifi, mobile, dialup, satellite, ?
# geo_continent_code : Two letter representation of the continent per UN M.49
# geo_country_name : country name per ISO 3166-1, lowercase
# geo_country_code : two letter representation of the country per ISO 3166-1 alpha-2
# geo_country_code3 : three letter representation of the country per ISO 3166-1 alpha-3
# geo_latitude : Latitude, in units of degrees from the equator. 999.9 for missing data
# geo_longitude : Longitude, in units of degrees from the IERS Reference Meridian. 999.9 for missing data
# geo_region_code : Two digit region code per ISO 3166-2. Typically paired with geo_country_code
# host : Either the original host requested or the host requested by the client (host header)
# request_body_size_bytes : Total body bytes read from the client generating the request.
# request_duration_usec : The time since the request started in microseconds.
# request_header_size_bytes : Total header bytes read from the client generating the request.
# request_method : HTTP method sent by the client
# request_next_router_prefetch : Next-Router-Prefetch request header, sent by the
#   Next.js app router on prefetch requests. Empty if absent.
# request_protocol : HTTP protocol version in use for this request.
# request_referer : HTTP referer as provided by the client
# request_user_agent : HTTP useragent as provided by the client


__base_fastly_log_format_string = """{
"client_ip":"%{json.escape(req.http.Fastly-Client-IP)}V",
"client_data":{"bot_name":"%{json.escape(client.bot.name)}V",
"browser_name":"%{json.escape(client.browser.name)}V",
"browser_version":"%{json.escape(client.browser.version)}V",
"class_bot":%{client.class.bot}V,
"class_browser":%{client.class.browser}V,
"class_checker":%{client.class.checker}V,
"class_downloader":%{client.class.downloader}V,
"class_feedreader":%{client.class.feedreader}V,
"class_filter":%{client.class.filter}V,
"class_masquerading":%{client.class.masquerading}V,
"class_spam":%{client.class.spam}V,
"display_height":%{client.display.height}V,
"display_width":%{client.display.width}V,
"display_ppi":%{client.display.ppi}V,
"display_touchscreen":%{client.display.touchscreen}V,
"platform_ereader":%{client.platform.ereader}V,
"platform_gameconsole":%{client.platform.gameconsole}V,
"platform_mediaplayer":%{client.platform.mediaplayer}V,
"platform_mobile":%{client.platform.mobile}V,
"platform_smarttv":%{client.platform.smarttv}V,
"platform_tablet":%{client.platform.tablet}V,
"platform_tvplayer":%{client.platform.tvplayer}V},
"fastly_is_edge":%{if(fastly.ff.visits_this_service==0,"true","false")}V,
"fastly_pop_identifier":"%{json.escape(server.datacenter)}V",
"fastly_server":"%{json.escape(server.identity)}V",
"fastly_background_fetch":%{req.is_background_fetch}V,
"geo_city":"%{json.escape(client.geo.city)}V",
"geo_conn_speed":"%{json.escape(client.geo.conn_speed)}V",
"geo_conn_type":"%{json.escape(client.geo.conn_type)}V",
"geo_continent_code":"%{json.escape(client.geo.continent_code)}V",
"geo_country_name":"%{json.escape(client.geo.country_name)}V",
"geo_country_code":"%{json.escape(client.geo.country_code)}V",
"geo_country_code3":"%{json.escape(client.geo.country_code3)}V",
"geo_latitude":%{client.geo.latitude}V,
"geo_longitude":%{client.geo.longitude}V,
"geo_region":"%{json.escape(client.geo.region.utf8)}V",
"host":"%{if(req.http.Fastly-Orig-Host,json.escape(req.http.Fastly-Orig-Host),json.escape(req.http.Host))}V",
"request_body_size_bytes":%{req.body_bytes_read}V,
"request_duration_usec":%{time.elapsed.usec}V,
"request_header_size_bytes":%{req.header_bytes_read}V,
"request_method":"%{json.escape(req.method)}V",
"request_next_router_prefetch":"%{json.escape(req.http.Next-Router-Prefetch)}V",
"request_protocol":"%{json.escape(req.proto)}V",
"request_referer":"%{json.escape(req.http.referer)}V",
"request_user_agent":"%{json.escape(req.http.User-Agent)}V",
"response_body_size_bytes":%{resp.body_bytes_written}V,
"response_header_size_bytes":%{resp.header_bytes_written}V,
"response_reason":%{if(resp.response,"%22"+json.escape(resp.response)+"%22","null")}V,
"response_state":"%{json.escape(fastly_info.state)}V",
"response_status":%{resp.status}V,
"timestamp":"%{strftime(\\{"%Y-%m-%dT%H:%M:%S%z"\\},time.start)}V",
"url":"%{json.escape(req.url)}V"}
"""


# A fastly logformat string isn't actually json and we can't treat it as such in code
# but we do need it to *produce* valid and minimized json at the end of the day once
# it is installed into the fastly log configurations.
def build_fastly_log_format_string(additional_static_fields: dict[str, str]) -> str:
    split_format_string = __base_fastly_log_format_string.split("\n")
    for key, value in additional_static_fields.items():
        split_format_string.insert(1, f'"{key}":"{value}",')
    return "".join(split_format_string)


def get_fastly_provider(
    wrap_in_pulumi_options: bool = True,  # noqa: FBT001, FBT002
) -> fastly.Provider | pulumi.ResourceOptions:
    pulumi.Config("fastly")
    fastly_provider = fastly.Provider(
        "fastly-provider",
        api_key=read_yaml_secrets(Path("fastly.yaml"))["admin_api_key"],
        opts=pulumi.ResourceOptions(
            aliases=[
                pulumi.Alias(name="default_5_0_0"),
                pulumi.Alias(name="default_4_0_4"),
            ],
        ),
    )
    if wrap_in_pulumi_options:
        fastly_provider = pulumi.ResourceOptions(provider=fastly_provider)
    return fastly_provider
