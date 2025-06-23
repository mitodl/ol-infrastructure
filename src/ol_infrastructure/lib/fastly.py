from pathlib import Path

import pulumi
import pulumi_fastly as fastly

from bridge.secrets.sops import read_yaml_secrets

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
