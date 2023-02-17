from pathlib import Path
from typing import Union

import pulumi
import pulumi_fastly as fastly

from bridge.secrets.sops import read_yaml_secrets


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


fastly_log_format_string = """{
    "timestamp": "%{strftime(\\{"%Y-%m-%dT%H:%M:%S%z"\\}, time.start)}V",
    "client_ip": "%{req.http.Fastly-Client-IP}V",
    "geo_country": "%{client.geo.country_name}V",
    "geo_city": "%{client.geo.city}V",
    "host": "%{if(req.http.Fastly-Orig-Host, req.http.Fastly-Orig-Host, req.http.Host)}V",
    "url": "%{json.escape(req.url)}V",
    "request_method": "%{json.escape(req.method)}V",
    "request_protocol": "%{json.escape(req.proto)}V",
    "request_referer": "%{json.escape(req.http.referer)}V",
    "request_user_agent": "%{json.escape(req.http.User-Agent)}V",
    "response_state": "%{json.escape(fastly_info.state)}V",
    "response_status": %{resp.status}V,
    "response_reason": %{if(resp.response, "%22"+json.escape(resp.response)+"%22", "null")}V,
    "response_body_size": %{resp.body_bytes_written}V,
    "fastly_server": "%{json.escape(server.identity)}V",
    "fastly_is_edge": %{if(fastly.ff.visits_this_service == 0, "true", "false")}V
}"""
