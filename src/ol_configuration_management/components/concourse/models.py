from enum import Enum
from ipaddress import IPv4Address, IPv6Address
from pathlib import Path
from typing import Optional, Text

from pydantic import BaseSettings, Field, SecretStr, validator


class IframeOptions(str, Enum):
    deny = "deny"
    same_origin = "sameorigin"


class ConcourseBaseConfig(BaseSettings):
    version: Text = "6.7.4"
    deploy_directory: Path = Path("/opt/concourse/")

    class Config:  # noqa: WPS431
        env_prefix = "concourse_"
        case_sensitive = False


class ConcourseWebConfig(ConcourseBaseConfig):
    admin_user: Text = Field("oldevops", concourse_env_var="CONCOURSE_ADD_LOCAL_USER")
    admin_password: SecretStr
    postgres_host: Text = Field(
        "concourse-postgres.service.consul", concourse_env_var="CONCOURSE_POSTGRES_HOST"
    )
    postgres_port: int = Field(5432, concourse_env_var="CONCOURSE_POSTGRES_PORT")
    database_name: Text = Field(
        "concourse", concourse_env_var="CONCOURSE_POSTGRES_DATABASE"
    )
    database_user: Text = Field("oldevops", concourse_env_var="CONCOURSE_POSTGRES_USER")
    database_password: SecretStr = Field(
        ..., concourse_env_var="CONCOURSE_POSTGRES_PASSWORD=my-password"
    )
    public_domain: Text = Field(..., concourse_env_var="CONCOURSE_EXTERNAL_URL")
    iframe_options: IframeOptions = Field(
        IframeOptions.deny, concourse_env_var="CONCOURSE_X_FRAME_OPTIONS"
    )
    peer_address: IPv4Address = Field(..., concourse_env_var="CONCOURSE_PEER_ADDRESS")
    cluster_name: Text = Field("", concourse_env_var="CONCOURSE_CLUSTER_NAME")
    session_signing_key: Path = Field(
        ..., concourse_env_var="CONCOURSE_SESSION_SIGNING_KEY"
    )
    tsa_host_key: Path = Field("...", concourse_env_var="CONCOURSE_TSA_HOST_KEY")
    authorized_keys_file: Path = Field(
        Path("/opt/concourse/authorized_keys"),
        concourse_env_var="CONCOURSE_TSA_AUTHORIZED_KEYS",
    )

    # Enable auditing for all api requests connected to builds.
    audit_builds: bool = Field(
        True, concourse_env_var="CONCOURSE_ENABLE_BUILD_AUDITING"
    )

    # Enable auditing for all api requests connected to containers.
    audit_containers: bool = Field(
        True, concourse_env_var="CONCOURSE_ENABLE_CONTAINER_AUDITING"
    )

    # Enable auditing for all api requests connected to jobs.
    audit_jobs: bool = Field(True, concourse_env_var="CONCOURSE_ENABLE_JOB_AUDITING")

    # Enable auditing for all api requests connected to pipelines.
    audit_pipelines: bool = Field(
        True, concourse_env_var="CONCOURSE_ENABLE_PIPELINE_AUDITING"
    )

    # Enable auditing for all api requests connected to resources.
    audit_resources: bool = Field(
        True, concourse_env_var="CONCOURSE_ENABLE_RESOURCE_AUDITING"
    )

    # Enable auditing for all api requests connected to system transactions.
    audit_system: bool = Field(
        True, concourse_env_var="CONCOURSE_ENABLE_SYSTEM_AUDITING"
    )

    # Enable auditing for all api requests connected to teams.
    audit_teams: bool = Field(True, concourse_env_var="CONCOURSE_ENABLE_TEAM_AUDITING")

    # Enable auditing for all api requests connected to workers.
    audit_workers: bool = Field(
        True, concourse_env_var="CONCOURSE_ENABLE_WORKER_AUDITING"
    )

    # Enable auditing for all api requests connected to volumes.
    audit_volumes: bool = Field(
        True, concourse_env_var="CONCOURSE_ENABLE_VOLUME_AUDITING"
    )

    github_client_id: Optional[Text] = Field(
        ..., concourse_env_var="CONCOURSE_GITHUB_CLIENT_ID"
    )
    github_client_secret: Optional[Text] = Field(
        ..., concourse_env_var="CONCOURSE_GITHUB_CLIENT_SECRET"
    )
    github_main_team_org: Optional[Text] = Field(
        "mitodl", concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_ORG"
    )
    github_main_team_concourse_team: Optional[Text] = Field(
        "mitodl:olengineering", concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_TEAM"
    )
    github_main_team_user: Text = Field(
        "odlbot", concourse_env_var="CONCOURSE_MAIN_TEAM_GITHUB_USER"
    )

    # 32 bit random string
    encryption_key: SecretStr = Field(
        ..., concourse_env_var="CONCOURSE_ENCRYPTION_KEY"
    )

    vault_url: Optional[Text] = Field(
        "https://active.vault.service.consul:8200",
        concourse_env_var="CONCOURSE_VAULT_URL",
    )
    vault_auth_backend: Optional[Text] = Field(
        "approle", concourse_env_var="CONCOURSE_VAULT_AUTH_BACKEND"
    )
    vault_auth_param: Optional[Text] = Field(
        ..., concourse_env_var="CONCOURSE_VAULT_AUTH_PARAM"
    )

    


    class Config:  # noqa: WPS431
        env_prefix = "concourse_web_"
        arbitrary_types_allowed = True
        case_sensitive = False

    @property
    def local_user(self):
        password_value = self.admin_password.get_secret_value()
        return f"{self.admin_user}:{password_value}"
