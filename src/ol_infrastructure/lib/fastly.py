from pathlib import Path
from typing import Union

import pulumi
import pulumi_fastly as fastly

from bridge.secrets.sops import read_yaml_secrets

__base_fastly_log_format_string = """{
"client_ip":"%{json.escape(req.http.Fastly-Client-IP)}V",
"fastly_is_edge":%{if(fastly.ff.visits_this_service==0,"true","false")}V,
"fastly_server":"%{json.escape(server.identity)}V",
"geo_city":"%{json.escape(client.geo.city)}V",
"geo_conn_speed":"%{json.escape(client.geo.conn_speed)}V",
"geo_conn_type":"%{json.escape(client.geo.conn_type)}V",
"geo_continent_code":"%{json.escape(client.geo.continent_code)}V",
"geo_country":"%{json.escape(client.geo.country_name)}V",
"geo_country_code":"%{json.escape(client.geo.country_code)}V",
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
"url":"%{json.escape(req.url)}V"
}"""


# A fastly logformat string isn't actually json and we can't treat it as such in code
# but we do need it to *produce* valid and minimized json at the end of the day once
# it is installed into the fastly log configurations.
def build_fastly_log_format_string(additional_static_fields: dict[str, str]) -> str:
    split_format_string = __base_fastly_log_format_string.split("\n")
    for key, value in additional_static_fields.items():
        split_format_string.insert(1, f'"{key}":"{value}",')
    return "".join(split_format_string)


def get_fastly_provider(
    wrap_in_pulumi_options: bool = True,
) -> Union[fastly.Provider, pulumi.ResourceOptions]:
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
