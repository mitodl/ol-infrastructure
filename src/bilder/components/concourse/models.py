import secrets
from enum import Enum
from functools import partial
from pathlib import Path
from typing import Dict, List, Optional

from pydantic import Field, PositiveInt, SecretStr, validator

from bilder.lib.model_helpers import OLBaseSettings
from bridge.lib.magic_numbers import DEFAULT_POSTGRES_PORT

CONCOURSE_ENCRYPTION_KEY_REQUIRED_LENGTH = 32


class IframeOptions(str, Enum):  # noqa: WPS600
    deny = "deny"
    same_origin = "sameorigin"


class ConcourseBaseConfig(OLBaseSettings):
    user: str = "concourse"
    version: str = "7.1.0"
    configuration_directory: Path = Path("/etc/concourse")
    deploy_directory: Path = Path("/opt/concourse/")
    data_directory: Path = Path("/var/lib/concourse")
    env_file_path: Path = Path("/etc/default/concourse")

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

    class Config:  # noqa: WPS431
        env_prefix = "concourse_"


class ConcourseWebConfig(ConcourseBaseConfig):
    _node_type: str = "web"
    admin_password: SecretStr
    admin_user: str = "admin"
    audit_builds: bool = (
        Field(  # Enable auditing for all api requests connected to builds.
            default=True, concourse_env_var="CONCOURSE_ENABLE_BUILD_AUDITING"
        )
    )
    audit_containers: bool = (
        Field(  # Enable auditing for all api requests connected to containers.
            default=True, concourse_env_var="CONCOURSE_ENABLE_CONTAINER_AUDITING"
        )
    )
    audit_jobs: bool = Field(  # Enable auditing for all api requests connected to jobs.
        default=True, concourse_env_var="CONCOURSE_ENABLE_JOB_AUDITING"
    )
    audit_pipelines: bool = (
        Field(  # Enable auditing for all api requests connected to pipelines.
            default=True, concourse_env_var="CONCOURSE_ENABLE_PIPELINE_AUDITING"
        )
    )
    audit_resources: bool = (
        Field(  # Enable auditing for all api requests connected to resources.
            default=True, concourse_env_var="CONCOURSE_ENABLE_RESOURCE_AUDITING"
        )
    )
    audit_system: bool = (
        Field(  # Enable auditing for all api requests connected to system transactions.
            default=True, concourse_env_var="CONCOURSE_ENABLE_SYSTEM_AUDITING"
        )
    )
    audit_teams: bool = (
        Field(  # Enable auditing for all api requests connected to teams.
            default=True, concourse_env_var="CONCOURSE_ENABLE_TEAM_AUDITING"
        )
    )
    audit_volumes: bool = (
        Field(  # Enable auditing for all api requests connected to volumes.
            default=True, concourse_env_var="CONCOURSE_ENABLE_VOLUME_AUDITING"
        )
    )
    audit_workers: bool = (
        Field(  # Enable auditing for all api requests connected to workers.
            default=True, concourse_env_var="CONCOURSE_ENABLE_WORKER_AUDITING"
        )
    )
    authorized_worker_keys: Optional[List[str]] = None
    authorized_keys_file: Path = Field(
        Path("/etc/concourse/authorized_keys"),
        concourse_env_var="CONCOURSE_TSA_AUTHORIZED_KEYS",
    )
    cluster_name: Optional[str] = Field(
        None, concourse_env_var="CONCOURSE_CLUSTER_NAME"
    )
    container_runtime: str = Field("containerd", concourse_env_var="CONCOURSE_RUNTIME")
    database_name: str = Field(
        "concourse", concourse_env_var="CONCOURSE_POSTGRES_DATABASE"
    )
    database_password: SecretStr = Field(
        ...,
        concourse_env_var="CONCOURSE_POSTGRES_PASSWORD",
        env_transform=lambda _: _.get_secret_value(),
    )
    database_user: str = Field("oldevops", concourse_env_var="CONCOURSE_POSTGRES_USER")
    database_sslmode: str = Field(
        "require", concourse_env_var="CONCOURSE_POSTGRES_SSLMODE"
    )
    db_max_conns_api: PositiveInt = Field(
        PositiveInt(10), concourse_env_var="CONCOURSE_API_MAX_CONNS"
    )
    db_max_conns_backend: PositiveInt = Field(
        PositiveInt(50), concourse_env_var="CONCOURSE_BACKEND_MAX_CONNS"  # noqa: WPS432
    )
    # Feature flag to enable pipeline grouping
    # https://concourse-ci.org/instanced-pipelines.html
    enable_instanced_pipelines: bool = Field(
        default=True, concourse_env_var="CONCOURSE_ENABLE_PIPELINE_INSTANCES"
    )
    encryption_key: SecretStr = Field(
        default_factory=partial(
            # Using floor division to produce an int instead of float
            secrets.token_hex,
            CONCOURSE_ENCRYPTION_KEY_REQUIRED_LENGTH // 2,
        ),
        concourse_env_var="CONCOURSE_ENCRYPTION_KEY",
        env_transform=lambda _: _.get_secret_value(),
    )  # 32 bit random string
    github_client_id: Optional[str] = Field(
        None, concourse_env_var="CONCOURSE_GITHUB_CLIENT_ID"
    )
    github_client_secret: Optional[str] = Field(
        None, concourse_env_var="CONCOURSE_GITHUB_CLIENT_SECRET"
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
    iframe_options: IframeOptions = Field(
        IframeOptions.deny, concourse_env_var="CONCOURSE_X_FRAME_OPTIONS"
    )
    peer_address: str = Field(
        "web.concourse.service.consul",
        concourse_env_var="CONCOURSE_PEER_ADDRESS",
    )
    postgres_host: str = Field(
        "concourse-postgres.service.consul", concourse_env_var="CONCOURSE_POSTGRES_HOST"
    )
    postgres_port: int = Field(
        DEFAULT_POSTGRES_PORT, concourse_env_var="CONCOURSE_POSTGRES_PORT"
    )
    public_domain: Optional[str] = Field(
        None, concourse_env_var="CONCOURSE_EXTERNAL_URL"
    )
    session_signing_key: Optional[str] = None
    session_signing_key_path: Path = Field(
        Path("/etc/concourse/session_signing_key"),
        concourse_env_var="CONCOURSE_SESSION_SIGNING_KEY",
    )
    tsa_host_key: Optional[str] = None
    tsa_host_key_path: Path = Field(
        Path("/etc/concourse/tsa_host_key"),
        concourse_env_var="CONCOURSE_TSA_HOST_KEY",
    )
    vault_auth_backend: Optional[str] = Field(
        "approle", concourse_env_var="CONCOURSE_VAULT_AUTH_BACKEND"
    )
    vault_auth_param: Optional[str] = Field(
        None, concourse_env_var="CONCOURSE_VAULT_AUTH_PARAM"
    )
    vault_ca_cert: Optional[Path] = Field(
        None, concourse_env_var="CONCOURSE_VAULT_CA_CERT"
    )
    vault_url: Optional[str] = Field(
        "https://active.vault.service.consul:8200",
        concourse_env_var="CONCOURSE_VAULT_URL",
    )

    class Config:  # noqa: WPS431
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
    tags: Optional[List[str]] = Field(
        None,
        concourse_env_var="CONCOURSE_TAG",
        env_transform=lambda _: ",".join(_),
    )
    tsa_host: str = Field(
        "web.concourse.service.consul:2222", concourse_env_var="CONCOURSE_TSA_HOST"
    )
    tsa_public_key: Optional[str]
    tsa_public_key_path: Path = Field(
        Path("/etc/concourse/tsa_host_key.pub"),
        concourse_env_var="CONCOURSE_TSA_PUBLIC_KEY",
    )
    work_dir: Path = Field(
        Path("/var/concourse/worker/"), concourse_env_var="CONCOURSE_WORK_DIR"
    )
    worker_private_key: Optional[str] = None
    worker_private_key_path: Path = Field(
        Path("/etc/concourse/worker_private_key.pem"),
        concourse_env_var="CONCOURSE_TSA_WORKER_PRIVATE_KEY",
    )

    class Config:  # noqa: WPS431
        env_prefix = "concourse_worker_"
