import secrets
from enum import Enum
from functools import partial
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Dict, Generator, List, Optional, Union

from pydantic import Field, PositiveInt, SecretStr, validator

from bilder.lib.model_helpers import OLBaseSettings
from bridge.lib.magic_numbers import (
    CONCOURSE_WEB_HOST_COMMUNICATION_PORT,
    DEFAULT_POSTGRES_PORT,
)

CONCOURSE_ENCRYPTION_KEY_REQUIRED_LENGTH = 32


class IframeOptions(str, Enum):  # noqa: WPS600
    deny = "deny"
    same_origin = "sameorigin"


class ConcourseBaseConfig(OLBaseSettings):
    user: str = "concourse"
    version: str = "7.4.0"
    configuration_directory: Path = Path("/etc/concourse")
    deploy_directory: Path = Path("/usr/local/concourse/")
    data_directory: Path = Path("/var/lib/concourse")
    env_file_path: Path = Path("/etc/default/concourse")

    def configuration_paths(self) -> Generator[Path, None, None]:
        """List the paths of files that are read by the Concourse process.

        :yields: A list of file paths that the Concourse process needs to read to
                  operate.

        :rtype: List[Path]
        """
        for field in self.__fields__.values():
            field_value = self.dict()[field.name]
            if field.type_ == Path and field_value:
                yield field_value

    def concourse_env(self) -> Dict[str, str]:
        """Create a mapping of concourse environment variables to the concrete values.

        :returns: A dictionary of concourse env vars and their values

        :rtype: Dict[Text, Text]
        """
        concourse_env_dict = {}
        for attr in self.__fields__.values():
            if attr.field_info.extra.get("concourse_env_var"):
                attr_val = self.dict()[attr.name]
                if attr_val is not None:
                    val_transform = attr.field_info.extra.get("env_transform") or str
                    concourse_env_dict[
                        attr.field_info.extra.get("concourse_env_var")
                    ] = val_transform(attr_val)
        return concourse_env_dict

    class Config:
        env_prefix = "concourse_"


class ConcourseWebConfig(ConcourseBaseConfig):
    _node_type: str = "web"
    admin_password: SecretStr
    admin_user: str = "admin"
    auth_duration: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AUTH_DURATION",
        description="Length of time for which tokens are valid. Afterwards, users will have to log back in. (default: 24h)",
    )
    authorized_keys_file: Path = Field(
        Path("/etc/concourse/authorized_keys"),
        concourse_env_var="CONCOURSE_TSA_AUTHORIZED_KEYS",
    )
    authorized_worker_keys: Optional[List[str]] = None
    aws_secretsmanager_access_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SECRETSMANAGER_ACCESS_KEY",
        description="AWS Access key ID",
    )
    aws_secretsmanager_pipeline_secret_template: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SECRETSMANAGER_PIPELINE_SECRET_TEMPLATE",
        description="AWS Secrets Manager secret identifier template used for pipeline specific parameter (default: /concourse/{{.Team}}/{{.Pipeline}}/{{.Secret}})",
    )
    aws_secretsmanager_region: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SECRETSMANAGER_REGION",
        description="AWS region to send requests to",
    )  # 32 bit random string
    aws_secretsmanager_secret_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SECRETSMANAGER_SECRET_KEY",
        description="AWS Secret Access Key",
    )
    aws_secretsmanager_session_token: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SECRETSMANAGER_SESSION_TOKEN",
        description="AWS Session Token",
    )
    aws_secretsmanager_team_secret_template: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SECRETSMANAGER_TEAM_SECRET_TEMPLATE",
        description="AWS Secrets Manager secret identifier  template used for team specific parameter (default: /concourse/{{.Team}}/{{.Secret}})",
    )
    aws_ssm_access_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SSM_ACCESS_KEY",
        description="AWS Access key ID",
    )
    aws_ssm_pipeline_secret_template: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SSM_PIPELINE_SECRET_TEMPLATE",
        description="AWS SSM parameter name template used for pipeline specific parameter (default: /concourse/{{.Team}}/{{.Pipeline}}/{{.Secret}})",
    )
    aws_ssm_region: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SSM_REGION",
        description="AWS region to send requests to",
    )
    aws_ssm_secret_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SSM_SECRET_KEY",
        description="AWS Secret Access Key",
    )
    aws_ssm_session_token: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SSM_SESSION_TOKEN",
        description="AWS Session Token",
    )
    aws_ssm_team_secret_template: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_AWS_SSM_TEAM_SECRET_TEMPLATE",
        description="AWS SSM parameter name template used for team specific parameter (default: /concourse/{{.Team}}/{{.Secret}})",
    )
    baggageclaim_response_header_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_RESPONSE_HEADER_TIMEOUT",
        description="How long to wait for Baggageclaim to send the response header. (default: 1m)",
    )
    base_resource_type_defaults: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BASE_RESOURCE_TYPE_DEFAULTS",
        description="Base resource type defaults",
    )
    bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BIND_IP",
        description="IP address on which to listen for web traffic. (default: 0.0.0.0)",
    )
    bind_port: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BIND_PORT",
        description="Port on which to listen for HTTP traffic. (default: 8080)",
    )
    build_tracker_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BUILD_TRACKER_INTERVAL",
        description="Interval on which to run build tracking. (default: 10s)",
    )
    capture_error_metrics: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_CAPTURE_ERROR_METRICS",
        description="Enable capturing of error log metrics",
    )
    cli_artifacts_dir: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CLI_ARTIFACTS_DIR",
        description="Directory containing downloadable CLI binaries.",
    )
    client_id: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CLIENT_ID",
        description="Client ID to use for login flow (default: concourse_web)",
    )
    client_secret: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CLIENT_SECRET",
        description="Client secret to use for login flow",
    )
    cluster_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CLUSTER_NAME",
        description="A name for this Concourse cluster, to be displayed on the dashboard page.",
    )
    component_runner_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_COMPONENT_RUNNER_INTERVAL",
        description="Interval on which runners are kicked off for builds, locks, scans, and checks (default: 10s)",
    )
    concurrent_request_limit: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONCURRENT_REQUEST_LIMIT",
        description="Limit the number of concurrent requests to an API endpoint (Example: ListAllJobs:5)",
    )
    config_rbac: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONFIG_RBAC",
        description="Customize RBAC role_action mapping.",
    )
    container_placement_strategy: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINER_PLACEMENT_STRATEGY",
        description="Method by which a worker is selected during container placement. If multiple methods are specified, they will be applied in order. Random strategy should only be used alone. (default: volume_locality)",
    )
    cookie_secure: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_COOKIE_SECURE",
        description="Force sending secure flag on http cookies",
    )
    database_name: str = Field(
        "concourse", concourse_env_var="CONCOURSE_POSTGRES_DATABASE"
    )
    database_password: SecretStr = Field(
        ...,
        concourse_env_var="CONCOURSE_POSTGRES_PASSWORD",
        env_transform=lambda _: _.get_secret_value(),
    )
    database_sslmode: str = Field(
        "require",
        concourse_env_var="CONCOURSE_POSTGRES_SSLMODE",
        description="Whether or not to use SSL. (default: disable)",
    )
    database_user: str = Field("oldevops", concourse_env_var="CONCOURSE_POSTGRES_USER")
    postgres_ca_cert: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_POSTGRES_CA_CERT",
        description="CA cert file location, to verify when connecting with SSL.",
    )
    postgres_client_cert: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_POSTGRES_CLIENT_CERT",
        description="Client cert file location.",
    )
    postgres_client_key: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_POSTGRES_CLIENT_KEY",
        description="Client key file location.",
    )
    postgres_connect_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_POSTGRES_CONNECT_TIMEOUT",
        description="Dialing timeout. (0 means wait indefinitely) (default: 5m)",
    )
    postgres_host: Optional[str] = Field(
        "concourse-postgres.service.consul",
        concourse_env_var="CONCOURSE_POSTGRES_HOST",
        description="The host to connect to. (default: 127.0.0.1)",
    )
    postgres_port: int = Field(
        DEFAULT_POSTGRES_PORT, concourse_env_var="CONCOURSE_POSTGRES_PORT"
    )
    postgres_socket: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_POSTGRES_SOCKET",
        description="Path to a UNIX domain socket to connect to.",
    )
    postgres_user: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_POSTGRES_USER",
        description="The user to sign in as.",
    )
    datadog_agent_host: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DATADOG_AGENT_HOST",
        description="Datadog agent host to expose dogstatsd metrics",
    )
    datadog_agent_port: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DATADOG_AGENT_PORT",
        description="Datadog agent port to expose dogstatsd metrics",
    )
    datadog_prefix: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DATADOG_PREFIX",
        description="Prefix for all metrics to easily find them in Datadog",
    )
    db_max_conns_api: PositiveInt = Field(
        PositiveInt(10),
        concourse_env_var="CONCOURSE_API_MAX_CONNS",
        description="The maximum number of open connections for the api connection pool. (default: 10)",
    )
    db_max_conns_backend: PositiveInt = Field(
        PositiveInt(50),
        concourse_env_var="CONCOURSE_BACKEND_MAX_CONNS",
        description="The maximum number of open connections for the backend connection pool. (default: 50)",
    )
    debug_bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DEBUG_BIND_IP",
        description="IP address on which to listen for the pprof debugger endpoints. (default: 127.0.0.1)",
    )
    debug_bind_port: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DEBUG_BIND_PORT",
        description="Port on which to listen for the pprof debugger endpoints. (default: 8079)",
    )
    default_build_logs_to_retain: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DEFAULT_BUILD_LOGS_TO_RETAIN",
        description="Default build logs to retain, 0 means all",
    )
    default_days_to_retain_build_logs: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DEFAULT_DAYS_TO_RETAIN_BUILD_LOGS",
        description="Default days to retain build logs. 0 means unlimited",
    )
    default_task_cpu_limit: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DEFAULT_TASK_CPU_LIMIT",
        description="Default max number of cpu shares per task, 0 means unlimited",
    )
    default_task_memory_limit: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DEFAULT_TASK_MEMORY_LIMIT",
        description="Default maximum memory per task, 0 means unlimited",
    )
    display_user_id_per_connector: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DISPLAY_USER_ID_PER_CONNECTOR",
        description="Define how to display user ID for each authentication connector. Format is <connector>:<fieldname>. Valid field names are user_id, name, username and email, where name maps to claims field username, and username maps to claims field preferred username",
    )
    emit_to_logs: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_EMIT_TO_LOGS",
        description="Emit metrics to logs.",
    )
    enable_across_step: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_ENABLE_ACROSS_STEP",
        description="Enable the experimental across step to be used in jobs. The API is subject to change.",
    )
    enable_build_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_BUILD_AUDITING",
        description="Enable auditing for all api requests connected to builds.",
    )
    enable_container_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_CONTAINER_AUDITING",
        description="Enable auditing for all api requests connected to containers.",
    )
    enable_global_resources: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_GLOBAL_RESOURCES",
        description="Enable equivalent resources across pipelines and teams to share a single version history.",
    )
    enable_job_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_JOB_AUDITING",
        description="Enable auditing for all api requests connected to jobs.",
    )
    enable_lets_encrypt: Optional[bool] = Field(
        default=None,
        concourse_env_var="CONCOURSE_ENABLE_LETS_ENCRYPT",
        description="Automatically configure TLS certificates via Let's Encrypt/ACME.",
    )
    enable_p2p_volume_streaming: Optional[bool] = Field(
        default=None,
        concourse_env_var="CONCOURSE_ENABLE_P2P_VOLUME_STREAMING",
        description="Enable P2P volume streaming",
    )
    enable_pipeline_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_PIPELINE_AUDITING",
        description="Enable auditing for all api requests connected to pipelines.",
    )
    # https://concourse-ci.org/instanced-pipelines.html
    enable_pipeline_instances: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_PIPELINE_INSTANCES",
        description="Enable pipeline instances",
    )
    enable_redact_secrets: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_REDACT_SECRETS",
        description="Enable redacting secrets in build logs.",
    )
    enable_rerun_when_worker_disappears: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_ENABLE_RERUN_WHEN_WORKER_DISAPPEARS",
        description="Enable automatically build rerun when worker disappears or a network error occurs",
    )
    enable_resource_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_RESOURCE_AUDITING",
        description="Enable auditing for all api requests connected to resources.",
    )
    enable_resource_causality: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_RESOURCE_CAUSALITY",
        description="Enable mapping of up and downstream dependencies for resources.",
    )
    enable_system_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_SYSTEM_AUDITING",
        description="Enable auditing for all api requests connected to system transactions.",
    )
    enable_team_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_TEAM_AUDITING",
        description="Enable auditing for all api requests connected to teams.",
    )
    enable_volume_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_VOLUME_AUDITING",
        description="Enable auditing for all api requests connected to volumes.",
    )
    enable_worker_auditing: Optional[bool] = Field(
        default=True,
        concourse_env_var="CONCOURSE_ENABLE_WORKER_AUDITING",
        description="Enable auditing for all api requests connected to workers.",
    )
    encryption_key: SecretStr = Field(
        default_factory=partial(
            # Using floor division to produce an int instead of float
            secrets.token_hex,
            CONCOURSE_ENCRYPTION_KEY_REQUIRED_LENGTH // 2,
        ),
        concourse_env_var="CONCOURSE_ENCRYPTION_KEY",
        env_transform=lambda _: _.get_secret_value(),
        description="A 16 or 32 length key used to encrypt sensitive information before storing it in the database.",
    )
    garden_request_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GARDEN_REQUEST_TIMEOUT",
        description="How long to wait for requests to Garden to complete. 0 means no timeout. (default: 5m)",
    )
    gc_check_recycle_period: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GC_CHECK_RECYCLE_PERIOD",
        description="Period after which to reap checks that are completed. (default: 1m)",
    )
    gc_failed_grace_period: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GC_FAILED_GRACE_PERIOD",
        description="Period after which failed containers will be garbage collected (default: 120h)",
    )
    gc_hijack_grace_period: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GC_HIJACK_GRACE_PERIOD",
        description="Period after which hijacked containers will be garbage collected (default: 5m)",
    )
    gc_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GC_INTERVAL",
        description="Interval on which to perform garbage collection. (default: 30s)",
    )
    gc_missing_grace_period: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GC_MISSING_GRACE_PERIOD",
        description="Period after which to reap containers and volumes that were created but went missing from the worker. (default: 5m)",
    )
    gc_one_off_grace_period: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GC_ONE_OFF_GRACE_PERIOD",
        description="Period after which one_off build containers will be garbage_collected. (default: 5m)",
    )
    gc_var_source_recycle_period: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GC_VAR_SOURCE_RECYCLE_PERIOD",
        description="Period after which to reap var_sources that are not used. (default: 5m)",
    )
    github_ca_cert: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GITHUB_CA_CERT",
        description="CA certificate of GitHub Enterprise deployment",
    )
    github_client_id: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GITHUB_CLIENT_ID",
        description="(Required) Client id",
    )
    github_client_secret: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GITHUB_CLIENT_SECRET",
        description="(Required) Client secret",
    )
    github_host: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GITHUB_HOST",
        description="Hostname of GitHub Enterprise deployment (No scheme, No trailing slash)",
    )
    github_main_team_concourse_team: Optional[str] = Field(
        "mitodl:odl-engineering", concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_TEAM"
    )
    github_main_team_org: Optional[str] = Field(
        "mitodl", concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_ORG"
    )
    github_main_team_user: str = Field(
        "odlbot", concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_USER"
    )
    gitlab_client_id: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GITLAB_CLIENT_ID",
        description="(Required) Client id",
    )
    gitlab_client_secret: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GITLAB_CLIENT_SECRET",
        description="(Required) Client secret",
    )
    gitlab_host: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GITLAB_HOST",
        description="Hostname of Gitlab Enterprise deployment (Include scheme, No trailing slash)",
    )
    global_resource_check_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GLOBAL_RESOURCE_CHECK_TIMEOUT",
        description="Time limit on checking for new versions of resources. (default: 1h)",
    )
    iframe_options: IframeOptions = Field(
        IframeOptions.deny,
        concourse_env_var="CONCOURSE_X_FRAME_OPTIONS",
        env_transform=str,
    )
    influxdb_batch_duration: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_INFLUXDB_BATCH_DURATION",
        description="The duration to wait before emitting a batch of points to InfluxDB, disregarding influxdb_batch_size. (default: 300s)",
    )
    influxdb_batch_size: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_INFLUXDB_BATCH_SIZE",
        description="Number of points to batch together when emitting to InfluxDB. (default: 5000)",
    )
    influxdb_database: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_INFLUXDB_DATABASE",
        description="InfluxDB database to write points to.",
    )
    influxdb_insecure_skip_verify: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_INFLUXDB_INSECURE_SKIP_VERIFY",
        description="Skip SSL verification when emitting to InfluxDB.",
    )
    influxdb_password: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_INFLUXDB_PASSWORD",
        description="InfluxDB server password.",
    )
    influxdb_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_INFLUXDB_URL",
        description="InfluxDB server address to emit points to.",
    )
    influxdb_username: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_INFLUXDB_USERNAME",
        description="InfluxDB server username.",
    )
    intercept_idle_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_INTERCEPT_IDLE_TIMEOUT",
        description="Length of time for a intercepted session to be idle before terminating. (default: 0m)",
    )
    job_scheduling_max_in_flight: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_JOB_SCHEDULING_MAX_IN_FLIGHT",
        description="Maximum number of jobs to be scheduling at the same time (default: 32)",
    )
    ldap_bind_dn: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_BIND_DN",
        description="(Required) Bind DN for searching LDAP users and groups. Typically this is a read_only user.",
    )
    ldap_bind_pw: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_BIND_PW",
        description="(Required) Bind Password for the user specified by 'bind_dn'",
    )
    ldap_ca_cert: Optional[str] = Field(
        None, concourse_env_var="CONCOURSE_LDAP_CA_CERT", description="CA certificate"
    )
    ldap_display_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_DISPLAY_NAME",
        description="The auth provider name displayed to users on the login page",
    )
    ldap_group_search_base_dn: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_GROUP_SEARCH_BASE_DN",
        description="BaseDN to start the search from. For example 'cn=groups,dc=example,dc=com'",
    )
    ldap_group_search_filter: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_GROUP_SEARCH_FILTER",
        description="Optional filter to apply when searching the directory. For example '(objectClass=posixGroup)'",
    )
    ldap_group_search_group_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_GROUP_SEARCH_GROUP_ATTR",
        description="Adds an additional requirement to the filter that an attribute in the group match the user's attribute value. The exact filter being added is: (<groupAttr>=<userAttr value>)",
    )
    ldap_group_search_name_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_GROUP_SEARCH_NAME_ATTR",
        description="The attribute of the group that represents its name.",
    )
    ldap_group_search_scope: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_GROUP_SEARCH_SCOPE",
        description="Can either be: 'sub' _ search the whole sub tree or 'one' _ only search one level. Defaults to 'sub'.",
    )
    ldap_group_search_user_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_GROUP_SEARCH_USER_ATTR",
        description="Adds an additional requirement to the filter that an attribute in the group match the user's attribute value. The exact filter being added is: (<groupAttr>=<userAttr value>)",
    )
    ldap_host: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_HOST",
        description="(Required) The host and optional port of the LDAP server. If port isn't supplied, it will be guessed based on the TLS configuration. 389 or 636.",
    )
    ldap_insecure_no_ssl: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_INSECURE_NO_SSL",
        description="Required if LDAP host does not use TLS.",
    )
    ldap_insecure_skip_verify: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_INSECURE_SKIP_VERIFY",
        description="Skip certificate verification",
    )
    ldap_start_tls: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_START_TLS",
        description="Start on insecure port, then negotiate TLS",
    )
    ldap_user_search_base_dn: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_USER_SEARCH_BASE_DN",
        description="BaseDN to start the search from. For example 'cn=users,dc=example,dc=com'",
    )
    ldap_user_search_email_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_USER_SEARCH_EMAIL_ATTR",
        description="A mapping of attributes on the user entry to claims. Defaults to 'mail'.",
    )
    ldap_user_search_filter: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_USER_SEARCH_FILTER",
        description="Optional filter to apply when searching the directory. For example '(objectClass=person)'",
    )
    ldap_user_search_id_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_USER_SEARCH_ID_ATTR",
        description="A mapping of attributes on the user entry to claims. Defaults to 'uid'.",
    )
    ldap_user_search_name_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_USER_SEARCH_NAME_ATTR",
        description="A mapping of attributes on the user entry to claims.",
    )
    ldap_user_search_scope: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_USER_SEARCH_SCOPE",
        description="Can either be: 'sub' _ search the whole sub tree or 'one' _ only search one level. Defaults to 'sub'.",
    )
    ldap_user_search_username: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_USER_SEARCH_USERNAME",
        description="Attribute to match against the inputted username. This will be translated and combined with the other filter as '(<attr>=<username>)'.",
    )
    ldap_username_prompt: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LDAP_USERNAME_PROMPT",
        description="The prompt when logging in through the UI when __password_connector=ldap. Defaults to 'Username'.",
    )
    lets_encrypt_acme_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LETS_ENCRYPT_ACME_URL",
        description="URL of the ACME CA directory endpoint. (default: https://acme_v02.api.letsencrypt.org/directory)",
    )
    lidar_scanner_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LIDAR_SCANNER_INTERVAL",
        description="Interval on which the resource scanner will run to see if new checks need to be scheduled (default: 10s)",
    )
    log_cluster_name: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_LOG_CLUSTER_NAME",
        description="Log cluster name.",
    )
    log_db_queries: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_LOG_DB_QUERIES",
        description="Log database queries.",
    )
    log_level: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_LOG_LEVEL",
        description="Minimum level of logs to see. (default: info)",
    )
    main_team_config: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_CONFIG",
        description="Configuration file for specifying team params",
    )
    main_team_github_org: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_ORG",
        description="A whitelisted GitHub org",
    )
    main_team_github_team: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_TEAM",
        description="A whitelisted GitHub team",
    )
    main_team_github_user: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_USER",
        description="A whitelisted GitHub user",
    )
    main_team_ldap_group: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_LDAP_GROUP",
        description="A whitelisted LDAP group",
    )
    main_team_ldap_user: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_LDAP_USER",
        description="A whitelisted LDAP user",
    )
    main_team_local_user: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_LOCAL_USER",
        description="A whitelisted local concourse user. These are the users you've added at web startup with the __add_local_user flag.",
    )
    main_team_oauth_group: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_OAUTH_GROUP",
        description="A whitelisted OAuth2 group",
    )
    main_team_oauth_user: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_OAUTH_USER",
        description="A whitelisted OAuth2 user",
    )
    main_team_oidc_group_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_OIDC_GROUP",
        description="A whitelisted OIDC group",
    )
    main_team_oidc_user: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_OIDC_USER",
        description="A whitelisted OIDC user",
    )
    main_team_saml_group_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_SAML_GROUP",
        description="A whitelisted SAML group",
    )
    main_team_saml_user: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAIN_TEAM_SAML_USER",
        description="A whitelisted SAML user",
    )
    max_active_containers_per_worker: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAX_ACTIVE_CONTAINERS_PER_WORKER",
        description="Maximum allowed number of active containers per worker. Has effect only when used with limit_active_containers placement strategy. 0 means no limit. (default: 0)",
    )
    max_active_tasks_per_worker: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAX_ACTIVE_TASKS_PER_WORKER",
        description="Maximum allowed number of active build tasks per worker. Has effect only when used with limit_active_tasks placement strategy. 0 means no limit. (default: 0)",
    )
    max_active_volumes_per_worker: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAX_ACTIVE_VOLUMES_PER_WORKER",
        description="Maximum allowed number of active volumes per worker. Has effect only when used with limit_active_volumes placement strategy. 0 means no limit. (default: 0)",
    )
    max_build_logs_to_retain: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_MAX_BUILD_LOGS_TO_RETAIN",
        description="Maximum build logs to retain, 0 means not specified. Will override values configured in jobs",
    )
    max_checks_per_second: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_MAX_CHECKS_PER_SECOND",
        description="Maximum number of checks that can be started per second. If not specified, this will be calculated as (# of resources)/(resource checking interval). -1 value will remove this maximum limit of checks per second.",
    )
    max_days_to_retain_build_logs: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_MAX_DAYS_TO_RETAIN_BUILD_LOGS",
        description="Maximum days to retain build logs, 0 means not specified. Will override values configured in jobs",
    )
    metrics_attribute: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_METRICS_ATTRIBUTE",
        description="A key_value attribute to attach to emitted metrics. Can be specified multiple times.",
    )
    metrics_buffer_size: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_METRICS_BUFFER_SIZE",
        description="The size of the buffer used in emitting event metrics. (default: 1000)",
    )
    metrics_host_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_METRICS_HOST_NAME",
        description="Host string to attach to emitted metrics.",
    )
    newrelic_account_id: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_NEWRELIC_ACCOUNT_ID",
        description="New Relic Account ID",
    )
    newrelic_api_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_NEWRELIC_API_KEY",
        description="New Relic Insights API Key",
    )
    newrelic_batch_disable_compression: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_NEWRELIC_BATCH_DISABLE_COMPRESSION",
        description="Disables compression of the batch before sending it",
    )
    newrelic_batch_duration: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_NEWRELIC_BATCH_DURATION",
        description="Length of time to wait between emitting until all currently batched events are emitted (default: 60s)",
    )
    newrelic_batch_size: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_NEWRELIC_BATCH_SIZE",
        description="Number of events to batch together before emitting (default: 2000)",
    )
    newrelic_insights_api_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_NEWRELIC_INSIGHTS_API_URL",
        description="Base Url for insights Insert API (default: https://insights_collector.newrelic.com)",
    )
    newrelic_service_prefix: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_NEWRELIC_SERVICE_PREFIX",
        description="An optional prefix for emitted New Relic events",
    )
    oauth_auth_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_AUTH_URL",
        description="(Required) Authorization URL",
    )
    oauth_ca_cert: Optional[str] = Field(
        None, concourse_env_var="CONCOURSE_OAUTH_CA_CERT", description="CA Certificate"
    )
    oauth_client_id: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_CLIENT_ID",
        description="(Required) Client id",
    )
    oauth_client_secret: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_CLIENT_SECRET",
        description="(Required) Client secret",
    )
    oauth_display_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_DISPLAY_NAME",
        description="The auth provider name displayed to users on the login page",
    )
    oauth_groups_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_GROUPS_KEY",
        description="The groups key indicates which claim to use to map external groups to Concourse teams. (default: groups)",
    )
    oauth_scope: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_SCOPE",
        description="Any additional scopes that need to be requested during authorization",
    )
    oauth_skip_ssl_validation: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_SKIP_SSL_VALIDATION",
        description="Skip SSL validation",
    )
    oauth_token_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_TOKEN_URL",
        description="(Required) Token URL",
    )
    oauth_user_id_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_USER_ID_KEY",
        description="The user id key indicates which claim to use to map an external user id to a Concourse user id. (default: user_id)",
    )
    oauth_user_name_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_USER_NAME_KEY",
        description="The user name key indicates which claim to use to map an external user name to a Concourse user name. (default: user_name)",
    )
    oauth_userinfo_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OAUTH_USERINFO_URL",
        description="(Required) UserInfo URL",
    )
    oidc_ca_cert: Optional[str] = Field(
        None, concourse_env_var="CONCOURSE_OIDC_CA_CERT", description="CA Certificate"
    )
    oidc_client_id: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_CLIENT_ID",
        description="(Required) Client id",
    )
    oidc_client_secret: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_CLIENT_SECRET",
        description="(Required) Client secret",
    )
    oidc_disable_groups: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_DISABLE_GROUPS",
        description="Disable OIDC groups claims",
    )
    oidc_display_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_DISPLAY_NAME",
        description="The auth provider name displayed to users on the login page",
    )
    oidc_groups_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_GROUPS_KEY",
        description="The groups key indicates which claim to use to map external groups to Concourse teams. (default: groups)",
    )
    oidc_hosted_domains: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_HOSTED_DOMAINS",
        description="List of whitelisted domains when using Google, only users from a listed domain will be allowed to log in",
    )
    oidc_issuer: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_ISSUER",
        description="(Required) An OIDC issuer URL that will be used to discover provider configuration using the .well_known/openid_configuration",
    )
    oidc_scope: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_SCOPE",
        description="Any additional scopes of [openid] that need to be requested during authorization. Default to [openid, profile, email].",
    )
    oidc_skip_email_verified_validation: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_SKIP_EMAIL_VERIFIED_VALIDATION",
        description="Ignore the email_verified claim from the upstream provider, treating all users as if email_verified were true.",
    )
    oidc_skip_ssl_validation: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_SKIP_SSL_VALIDATION",
        description="Skip SSL validation",
    )
    oidc_user_name_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OIDC_USER_NAME_KEY",
        description="The user name key indicates which claim to use to map an external user name to a Concourse user name. (default: username)",
    )
    old_encryption_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OLD_ENCRYPTION_KEY",
        description="Encryption key previously used for encrypting sensitive information. If provided without a new key, data is encrypted. If provided with a new key, data is re_encrypted.",
    )
    opa_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OPA_TIMEOUT",
        description="OPA request timeout. (default: 5s)",
    )
    opa_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_OPA_URL",
        description="OPA policy check endpoint.",
    )
    p2p_volume_streaming_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_P2P_VOLUME_STREAMING_TIMEOUT",
        description="Timeout value of p2p volume streaming (default: 15m)",
    )
    password_connector: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_PASSWORD_CONNECTOR",
        description="Connector to use when authenticating via 'fly login -u ... -p ...' (default: local)",
    )
    peer_address: Optional[str] = Field(
        "web.concourse.service.consul",
        concourse_env_var="CONCOURSE_PEER_ADDRESS",
        description="Network address of this web node, reachable by other web nodes. Used for forwarded worker addresses. (default: 127.0.0.1)",
    )
    policy_check_filter_action: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_POLICY_CHECK_FILTER_ACTION",
        description="Actions in the list will go through policy check",
    )
    policy_check_filter_action_skip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_POLICY_CHECK_FILTER_ACTION_SKIP",
        description="Actions the list will not go through policy check",
    )
    policy_check_filter_http_method: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_POLICY_CHECK_FILTER_HTTP_METHOD",
        description="API http method to go through policy check",
    )
    prometheus_bind_ip: Optional[Union[IPv4Address, IPv6Address]] = Field(
        None,
        concourse_env_var="CONCOURSE_PROMETHEUS_BIND_IP",
        description="IP to listen on to expose Prometheus metrics.",
    )
    prometheus_bind_port: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_PROMETHEUS_BIND_PORT",
        description="Port to listen on to expose Prometheus metrics.",
    )
    public_domain: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_EXTERNAL_URL",
        description="URL used to reach any ATC from the outside world.",
    )
    resource_checking_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_RESOURCE_CHECKING_INTERVAL",
        description="Interval on which to check for new versions of resources. (default: 1m)",
    )
    resource_with_webhook_checking_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_RESOURCE_WITH_WEBHOOK_CHECKING_INTERVAL",
        description="Interval on which to check for new versions of resources that has webhook defined. (default: 1m)",
    )
    saml_ca_cert: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_CA_CERT",
        description="(Required) CA Certificate",
    )
    saml_display_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_DISPLAY_NAME",
        description="The auth provider name displayed to users on the login page",
    )
    saml_email_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_EMAIL_ATTR",
        description="The email indicates which claim to use to map an external user email to a Concourse user email. (default: email)",
    )
    saml_entity_issuer: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_ENTITY_ISSUER",
        description="Manually specify dex's Issuer value.",
    )
    saml_groups_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_GROUPS_ATTR",
        description="The groups key indicates which attribute to use to map external groups to Concourse teams. (default: groups)",
    )
    saml_groups_delim: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_GROUPS_DELIM",
        description="If specified, groups are returned as string, this delimiter will be used to split the group string.",
    )
    saml_name_id_policy_format: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_NAME_ID_POLICY_FORMAT",
        description="Requested format of the NameID. The NameID value is is mapped to the ID Token 'sub' claim.",
    )
    saml_skip_ssl_validation: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_SKIP_SSL_VALIDATION",
        description="Skip SSL validation",
    )
    saml_sso_issuer: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_SSO_ISSUER",
        description="Issuer value expected in the SAML response.",
    )
    saml_sso_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_SSO_URL",
        description="(Required) SSO URL used for POST value",
    )
    saml_username_attr: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SAML_USERNAME_ATTR",
        description="The user name indicates which claim to use to map an external user name to a Concourse user name. (default: name)",
    )
    secret_cache_duration: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SECRET_CACHE_DURATION",
        description="If the cache is enabled, secret values will be cached for not longer than this duration (it can be less, if underlying secret lease time is smaller) (default: 1m)",
    )
    secret_cache_duration_notfound: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SECRET_CACHE_DURATION_NOTFOUND",
        description="If the cache is enabled, secret not found responses will be cached for this duration (default: 10s)",
    )
    secret_cache_enabled: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_SECRET_CACHE_ENABLED",
        description="Enable in_memory cache for secrets",
    )
    secret_cache_purge_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SECRET_CACHE_PURGE_INTERVAL",
        description="If the cache is enabled, expired items will be removed on this interval (default: 10m)",
    )
    secret_retry_attempts: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SECRET_RETRY_ATTEMPTS",
        description="The number of attempts secret will be retried to be fetched, in case a retryable error happens. (default: 5)",
    )
    secret_retry_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SECRET_RETRY_INTERVAL",
        description="The interval between secret retry retrieval attempts. (default: 1s)",
    )
    session_signing_key: Optional[str] = None
    session_signing_key_path: Path = Field(
        Path("/etc/concourse/session_signing_key"),
        concourse_env_var="CONCOURSE_SESSION_SIGNING_KEY",
        description="File containing an RSA private key, used to sign auth tokens.",
    )
    streaming_artifacts_compression: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_STREAMING_ARTIFACTS_COMPRESSION",
        description="Compression algorithm for internal streaming. (default: gzip)",
    )
    syslog_address: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SYSLOG_ADDRESS",
        description="Remote syslog server address with port (Example: 0.0.0.0:514).",
    )
    syslog_ca_cert: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SYSLOG_CA_CERT",
        description="Paths to PEM_encoded CA cert files to use to verify the Syslog server SSL cert.",
    )
    syslog_drain_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SYSLOG_DRAIN_INTERVAL",
        description="Interval over which checking is done for new build logs to send to syslog server (duration measurement units are s/m/h; eg. 30s/30m/1h) (default: 30s)",
    )
    syslog_hostname: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SYSLOG_HOSTNAME",
        description="Client hostname with which the build logs will be sent to the syslog server. (default: atc_syslog_drainer)",
    )
    syslog_transport: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SYSLOG_TRANSPORT",
        description="Transport protocol for syslog messages (Currently supporting tcp, udp & tls).",
    )
    system_claim_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SYSTEM_CLAIM_KEY",
        description="The token claim key to use when matching system_claim_values (default: aud)",
    )
    system_claim_value: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SYSTEM_CLAIM_VALUE",
        description="Configure which token requests should be considered 'system' requests. (default: concourse_worker)",
    )
    tls_bind_port: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TLS_BIND_PORT",
        description="Port on which to listen for HTTPS traffic.",
    )
    tls_ca_cert: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TLS_CA_CERT",
        description="File containing the client CA certificate, enables mTLS",
    )
    tls_cert: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TLS_CERT",
        description="File containing an SSL certificate.",
    )
    tls_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TLS_KEY",
        description="File containing an RSA private key, used to encrypt HTTPS traffic.",
    )
    tracing_attribute: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_ATTRIBUTE",
        description="attributes to attach to traces as metadata",
    )
    tracing_honeycomb_api_key: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_HONEYCOMB_API_KEY",
        description="honeycomb.io api key",
    )
    tracing_honeycomb_dataset: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_HONEYCOMB_DATASET",
        description="honeycomb.io dataset name",
    )
    tracing_honeycomb_service_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_HONEYCOMB_SERVICE_NAME",
        description="honeycomb.io service name (default: concourse)",
    )
    tracing_jaeger_endpoint: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_JAEGER_ENDPOINT",
        description="jaeger http_based thrift collector",
    )
    tracing_jaeger_service: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_JAEGER_SERVICE",
        description="jaeger process service name (default: web)",
    )
    tracing_jaeger_tags: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_JAEGER_TAGS",
        description="tags to add to the components",
    )
    tracing_otlp_address: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_OTLP_ADDRESS",
        description="otlp address to send traces to",
    )
    tracing_otlp_header: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_OTLP_HEADER",
        description="headers to attach to each tracing message",
    )
    tracing_otlp_use_tls: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_OTLP_USE_TLS",
        description="whether to use tls or not",
    )
    tracing_service_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_SERVICE_NAME",
        description="service name to attach to traces as metadata (default: concourse_web)",
    )
    tracing_stackdriver_projectid: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TRACING_STACKDRIVER_PROJECTID",
        description="GCP's Project ID",
    )
    tsa_atc_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_ATC_URL",
        description="ATC API endpoints to which workers will be registered.",
    )
    tsa_authorized_keys: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_AUTHORIZED_KEYS",
        description="Path to file containing keys to authorize, in SSH authorized_keys format (one public key per line).",
    )
    tsa_bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_BIND_IP",
        description="IP address on which to listen for SSH. (default: 0.0.0.0)",
    )
    tsa_bind_port: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_BIND_PORT",
        description="Port on which to listen for SSH. (default: 2222)",
    )
    tsa_client_id: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_CLIENT_ID",
        description="Client used to fetch a token from the auth server. NOTE: if you change this value you will also need to change the __system_claim_value flag so the atc knows to allow requests from this client. (default: concourse_worker)",
    )
    tsa_client_secret: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_CLIENT_SECRET",
        description="Client used to fetch a token from the auth server",
    )
    tsa_cluster_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_CLUSTER_NAME",
        description="A name for this Concourse cluster, to be displayed on the dashboard page.",
    )
    tsa_debug_bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_DEBUG_BIND_IP",
        description="IP address on which to listen for the pprof debugger endpoints. (default: 127.0.0.1)",
    )
    tsa_debug_bind_port: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_DEBUG_BIND_PORT",
        description="Port on which to listen for the pprof debugger endpoints. (default: 2221)",
    )
    tsa_garden_request_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_GARDEN_REQUEST_TIMEOUT",
        description="How long to wait for requests to Garden to complete. 0 means no timeout. (default: 5m)",
    )
    tsa_heartbeat_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_HEARTBEAT_INTERVAL",
        description="interval on which to heartbeat workers to the ATC (default: 30s)",
    )
    tsa_host_key: Optional[str] = None
    tsa_host_key_path: Path = Field(
        Path("/etc/concourse/tsa_host_key"),
        concourse_env_var="CONCOURSE_TSA_HOST_KEY",
        description="Path to private key to use for the SSH server.",
    )
    tsa_log_cluster_name: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_LOG_CLUSTER_NAME",
        description="Log cluster name.",
    )
    tsa_log_level: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_LOG_LEVEL",
        description="Minimum level of logs to see. (default: info)",
    )
    tsa_peer_address: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_PEER_ADDRESS",
        description="Network address of this web node, reachable by other web nodes. Used for forwarded worker addresses. (default: 127.0.0.1)",
    )
    tsa_scope: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_SCOPE",
        description="Scopes to request from the auth server",
    )
    tsa_team_authorized_keys: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_TEAM_AUTHORIZED_KEYS",
        description="Path to file containing keys to authorize, in SSH authorized_keys format (one public key per line).",
    )
    tsa_team_authorized_keys_file: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_TEAM_AUTHORIZED_KEYS_FILE",
        description="Path to file containing a YAML array of teams and their authorized SSH keys, e.g. [{team:foo,ssh_keys:[key1,key2]}].",
    )
    tsa_token_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TSA_TOKEN_URL",
        description="Token endpoint of the auth server",
    )
    vault_auth_backend: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_AUTH_BACKEND",
        description="Auth backend to use for logging in to Vault.",
    )
    vault_auth_backend_max_ttl: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_AUTH_BACKEND_MAX_TTL",
        description="Time after which to force a re_login. If not set, the token will just be continuously renewed.",
    )
    vault_auth_param: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_AUTH_PARAM",
        description="Paramter to pass when logging in via the backend. Can be specified multiple times.",
    )
    vault_ca_cert: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_CA_CERT",
        description="Path to a PEM_encoded CA cert file to use to verify the vault server SSL cert.",
    )
    vault_ca_path: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_CA_PATH",
        description="Path to a directory of PEM_encoded CA cert files to verify the vault server SSL cert.",
    )
    vault_client_cert: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_CLIENT_CERT",
        description="Path to the client certificate for Vault authorization.",
    )
    vault_client_key: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_CLIENT_KEY",
        description="Path to the client private key for Vault authorization.",
    )
    vault_client_token: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_CLIENT_TOKEN",
        description="Client token for accessing secrets within the Vault server.",
    )
    vault_insecure_skip_verify: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_INSECURE_SKIP_VERIFY",
        description="Enable insecure SSL verification.",
    )
    vault_login_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_LOGIN_TIMEOUT",
        description="Timeout value for Vault login. (default: 60s)",
    )
    vault_lookup_templates: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_LOOKUP_TEMPLATES",
        description="Path templates for credential lookup (default: /{{.Team}}/{{.Pipeline}}/{{.Secret}}, /{{.Team}}/{{.Secret}})",
    )
    vault_namespace: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_NAMESPACE",
        description="Vault namespace to use for authentication and secret lookup.",
    )
    vault_path_prefix: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_PATH_PREFIX",
        description="Path under which to namespace credential lookup. (default: /concourse)",
    )
    vault_query_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_QUERY_TIMEOUT",
        description="Timeout value for Vault query. (default: 60s)",
    )
    vault_retry_initial: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_RETRY_INITIAL",
        description="The initial time between retries when logging in or re_authing a secret. (default: 1s)",
    )
    vault_retry_max: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_RETRY_MAX",
        description="The maximum time between retries when logging in or re_authing a secret. (default: 5m)",
    )
    vault_server_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_SERVER_NAME",
        description="If set, is used to set the SNI host when connecting via TLS.",
    )
    vault_shared_path: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VAULT_SHARED_PATH",
        description="Path under which to lookup shared credentials.",
    )
    vault_url: Optional[str] = Field(
        "https://active.vault.service.consul:8200",
        concourse_env_var="CONCOURSE_VAULT_URL",
        description="Vault server address used to access secrets.",
    )
    web_public_dir: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_WEB_PUBLIC_DIR",
        description="Web public/ directory to serve live for local development.",
    )

    class Config:
        env_prefix = "concourse_web_"
        arbitrary_types_allowed = True

    @validator("encryption_key")
    def validate_encryption_key_length(cls, encryption_key):  # noqa: N805
        if len(encryption_key) != CONCOURSE_ENCRYPTION_KEY_REQUIRED_LENGTH:
            raise ValueError(
                "Encryption key is not the correct length. "
                "It needs to be a 32 byte random string."
            )
        return encryption_key

    @property
    def local_user(self):
        password_value = self.admin_password.get_secret_value()
        return f"{self.admin_user}:{password_value}"

    def concourse_env(self) -> Dict[str, str]:
        concourse_env_dict = super().concourse_env()
        concourse_env_dict["CONCOURSE_ADD_LOCAL_USER"] = self.local_user
        return concourse_env_dict


class ConcourseWorkerConfig(ConcourseBaseConfig):
    _node_type: str = "worker"
    user: str = "root"
    ## TODO: MAD 02162022 Correct this deploy_directory reference.
    ## This 'works' but it isn't correct. It will always reference the default value for
    ## deploy_directory, rather than any that is overridden in the actual instantiation, whereever
    ## that may be. Be aware.
    resource_types_directory: Path = (
        ConcourseBaseConfig().deploy_directory / "resource-types"
    )

    baggageclaim_bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_BIND_IP",
        description="IP address on which to listen for API traffic. "
        "(default: 127.0.0.1)",
    )
    baggageclaim_bind_port: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_BIND_PORT",
        description="Port on which to listen for API traffic. (default: 7788)",
    )
    baggageclaim_btrfs_binary: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_BTRFS_BIN",
        description="Path to btrfs binary (default: btrfs)",
    )
    baggageclaim_debug_bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_DEBUG_BIND_IP",
        description="IP address on which to listen for the pprof debugger endpoints. "
        "(default: 127.0.0.1)",
    )
    baggageclaim_debug_bind_port: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_DEBUG_BIND_PORT",
        description="Port on which to listen for the pprof debugger endpoints. "
        "(default: 7787)",
    )
    baggageclaim_disable_user_namespaces: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_DISABLE_USER_NAMESPACES",
        description="Disable remapping of user/group IDs in unprivileged volumes.",
    )
    baggageclaim_driver: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_DRIVER",
        description="Driver to use for managing volumes. (default: detect)",
    )
    baggageclaim_log_level: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_LOG_LEVEL",
        description="Minimum level of logs to see. (default: info)",
    )
    baggageclaim_mkfs_binary: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_MKFS_BIN",
        description="Path to mkfs.btrfs binary (default: mkfs.btrfs)",
    )
    baggageclaim_overlays_dir: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_OVERLAYS_DIR",
        description="Path to directory in which to store overlay data",
    )
    baggageclaim_p2p_interface_family: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_P2P_INTERFACE_FAMILY",
        description="4 for IPv4 and 6 for IPv6 (default: 4)",
    )
    baggageclaim_p2p_interface_name_pattern: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_P2P_INTERFACE_NAME_PATTERN",
        description="Regular expression to match a network interface for p2p streaming "
        "(default: eth0)",
    )
    baggageclaim_volumes: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_BAGGAGECLAIM_VOLUMES",
        description="Directory in which to place volume data.",
    )
    bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_BIND_IP",
        description="IP address on which to listen for the Garden server. "
        "(default: 127.0.0.1)",
    )
    certs_dir: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_CERTS_DIR",
        description="Directory to use when creating the resource certificates volume.",
    )
    connection_drain_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONNECTION_DRAIN_TIMEOUT",
        description="Duration after which a worker should give up draining forwarded "
        "connections on shutdown. (default: 1h)",
    )
    container_runtime: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_RUNTIME",
        description="Runtime to use with the worker. Please note that Houdini is "
        "insecure and doesn't run 'tasks' in containers. (default: guardian)",
    )
    container_sweeper_max_in_flight: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINER_SWEEPER_MAX_IN_FLIGHT",
        description="Maximum number of containers which can be swept in parallel. "
        "(default: 5)",
    )
    containerd_binary: Optional[Path] = Field(
        Path("/usr/local/concourse/bin/containerd"),
        concourse_env_var="CONCOURSE_CONTAINERD_BIN",
        description="Path to a containerd executable (non-absolute names get resolved "
        "from $PATH).",
    )
    containerd_cni_plugins_dir: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_CNI_PLUGINS_DIR",
        description="Path to CNI network plugins. (default: /usr/local/concourse/bin)",
    )
    containerd_config: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_CONFIG",
        description="Path to a config file to use for the Containerd daemon.",
    )
    containerd_dns_server: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_DNS_SERVER",
        description="DNS server IP address to use instead of automatically determined "
        "servers. Can be specified multiple times.",
    )
    containerd_enable_dns_proxy: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_DNS_PROXY_ENABLE",
        description="Enable proxy DNS server.",
    )
    containerd_external_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_EXTERNAL_IP",
        description="IP address to use to reach container's mapped ports. Autodetected "
        "if not specified.",
    )
    containerd_init_binary: Optional[Path] = Field(
        Path("/usr/local/concourse/bin/init"),
        concourse_env_var="CONCOURSE_CONTAINERD_INIT_BIN",
        description="Path to an init executable (non-absolute names get resolved from "
        "$PATH). (default: /usr/local/concourse/bin/init)",
    )
    containerd_max_containers: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_MAX_CONTAINERS",
        description="Max container capacity. 0 means no limit. (default: 250)",
    )
    containerd_mtu: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_MTU",
        description="MTU size for container network interfaces. Defaults to the MTU "
        "of the interface used for outbound access by the host.",
    )
    containerd_network_pool: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_NETWORK_POOL",
        description="Network range to use for dynamically allocated container subnets. "
        "(default: 10.80.0.0/16)",
    )
    containerd_plugins_dir: Optional[Path] = Field(
        Path("/usr/local/concourse/bin/"),
        concourse_env_var="CONCOURSE_CONTAINERD_CNI_PLUGINS_DIR",
    )
    containerd_request_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_REQUEST_TIMEOUT",
        description="How long to wait for requests to Containerd to complete. "
        "0 means no timeout. (default: 5m)",
    )
    containerd_restricted_network: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_CONTAINERD_RESTRICTED_NETWORK",
        description="Network ranges to which traffic from containers will be "
        "restricted. Can be specified multiple times.",
    )
    debug_bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_DEBUG_BIND_IP",
        description="IP address on which to listen for the pprof debugger endpoints. "
        "(default: 127.0.0.1)",
    )
    debug_bind_port: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_DEBUG_BIND_PORT",
        description="Port on which to listen for the pprof debugger endpoints. "
        "(default: 7776)",
    )
    ephemeral: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_EPHEMERAL",
        description="If set, the worker will be immediately removed upon stalling.",
    )
    external_garden_url: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_EXTERNAL_GARDEN_URL",
        description="API endpoint of an externally managed Garden server to use "
        "instead of running the embedded Garden server.",
    )
    garden_binary: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_GARDEN_BIN",
        description="Path to a garden server executable (non-absolute names get "
        "resolved from $PATH).",
    )
    garden_bind_port: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_BIND_PORT",
        description="Port on which to listen for the Garden server. (default: 7777)",
    )
    garden_config: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_GARDEN_CONFIG",
        description="Path to a config file to use for the Garden backend. "
        "e.g. 'foo-bar=a,b' for '--foo-bar a --foo-bar b'.",
    )
    garden_dns_server: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GARDEN_DNS_SERVER",
        description="DNS server IP address to use instead of automatically determined "
        "servers.",
    )
    garden_enable_dns_proxy: Optional[bool] = Field(
        None,
        concourse_env_var="CONCOURSE_GARDEN_DNS_PROXY_ENABLE",
        description="Enable proxy DNS server.",
    )
    garden_max_containers: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_GARDEN_MAX_CONTAINERS",
        description="Maximum container capacity. 0 means no limit. (default:250)",
    )
    garden_network_pool: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GARDEN_NETWORK_POOL",
        description="Network range to use for dynamically allocated container subnets. "
        "(default:10.80.0.0/16)",
    )
    garden_request_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_GARDEN_REQUEST_TIMEOUT",
        description="How long to wait for requests to the Garden server to complete. "
        "0 means no timeout. (default: 5m)",
    )
    healthcheck_bind_ip: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_HEALTHCHECK_BIND_IP",
        description="IP address on which to listen for health checking requests. "
        "(default: 0.0.0.0)",
    )
    healthcheck_bind_port: Optional[int] = Field(
        None,
        concourse_env_var="CONCOURSE_HEALTHCHECK_BIND_PORT",
        description="Port on which to listen for health checking requests. "
        "(default: 8888)",
    )
    healthcheck_timeout: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_HEALTHCHECK_TIMEOUT",
        description="HTTP timeout for the full duration of health checking. "
        "(default: 5s)",
    )
    log_level: Optional[str] = Field(
        "info",
        concourse_env_var="CONCOURSE_LOG_LEVEL",
        description="Minimum level of logs to see. (default: info)",
    )
    prepackaged_resources: Optional[List[str]] = Field(
        None,
        description="A list of resource names to pull "
        "from s3://ol-eng-artifacts/bundled-concourse-resources/.",
    )
    rebalance_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_REBALANCE_INTERVAL",
        description="Duration after which the registration should be swapped to "
        "another random SSH gateway. (default: 4h)",
    )
    resource_types: Optional[Path] = Field(
        None,
        concourse_env_var="CONCOURSE_RESOURCE_TYPES",
        description="Path to directory containing resource types the worker should "
        "advertise.",
    )
    sweep_interval: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_SWEEP_INTERVAL",
        description="Interval on which containers and volumes will be garbage "
        "collected from the worker. (default: 30s)",
    )
    tags: Optional[List[str]] = Field(
        None,
        concourse_env_var="CONCOURSE_TAG",
        env_transform=lambda _: ",".join(_),
        description="Tags to set during registration.",
    )
    team: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_TEAM",
        description="The name of the team that this worker will be assigned to.",
    )
    tsa_host: str = Field(
        f"web.concourse.service.consul:{CONCOURSE_WEB_HOST_COMMUNICATION_PORT}",
        concourse_env_var="CONCOURSE_TSA_HOST",
        description="TSA host to forward the worker through. (default: 127.0.0.1:2222)",
    )
    tsa_public_key: Optional[str]
    tsa_public_key_path: Path = Field(
        Path("/etc/concourse/tsa_host_key.pub"),
        concourse_env_var="CONCOURSE_TSA_PUBLIC_KEY",
        description="File containing a public key to expect from the TSA.",
    )
    volume_sweeper_max_in_flight: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_VOLUME_SWEEPER_MAX_IN_FLIGHT",
        description="Maximum number of volumes which can be swept in parallel. "
        "(default: 3)",
    )
    work_dir: Path = Field(
        Path("/var/concourse/worker/"),
        concourse_env_var="CONCOURSE_WORK_DIR",
        description="Directory in which to place container data.",
    )
    worker_name: Optional[str] = Field(
        None,
        concourse_env_var="CONCOURSE_NAME",
        description="The name to set for the worker during registration. "
        "If not specified, the hostname will be used.",
    )
    worker_private_key: Optional[str] = None
    worker_private_key_path: Path = Field(
        Path("/etc/concourse/worker_private_key.pem"),
        concourse_env_var="CONCOURSE_TSA_WORKER_PRIVATE_KEY",
        description="File containing the private key to use when authenticating to "
        "the TSA.",
    )

    class Config:
        env_prefix = "concourse_worker_"
