"""What every writer to the telemetry archive (ADR-0014) has to agree on.

The archive stack (`applications/observability_archive`) builds the endpoint;
the k8s-monitoring collectors, vector-log-proxy and the EC2 hosts running
Vector and Alloy write to it. This module is the one place the hostname, the
push paths, the credential file and the shape of a bounded Vector sink are
spelled out, so a writer cannot drift from the endpoint it ships to.

Only QA and Production have an archive (plan D1). A writer in another
environment gets `None` from `push_target()` and ships to Grafana Cloud alone.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from bridge.secrets.sops import read_yaml_secrets

ARCHIVE_DOMAINS: dict[str, str] = {
    "qa": "telemetry-archive-qa.ol.mit.edu",
    "production": "telemetry-archive.ol.mit.edu",
}

# Loki's native push API and its OTLP receiver. OTLP clients append /v1/logs
# themselves, so they are given the /otlp prefix.
LOKI_PUSH_PATH = "/loki/api/v1/push"
OTLP_LOGS_PATH = "/otlp/v1/logs"
OTLP_BASE_PATH = "/otlp"

# Grafana Cloud datasource UIDs for the archive. Fixed (plan D9) so a dashboard
# or a test can name them; never valid in an alert rule (D13).
ARCHIVE_LOKI_DATASOURCE_UID = "archive-loki"
ARCHIVE_MIMIR_DATASOURCE_UID = "archive-mimir"

# Environment variables the EC2 Vector and Alloy configs read.
VECTOR_USER_ENV = "ARCHIVE_LOKI_USER"
VECTOR_PASSWORD_ENV = "ARCHIVE_LOKI_PASSWORD"  # noqa: S105  # pragma: allowlist secret
ALLOY_ENDPOINT_ENV = "ARCHIVE_OTLP_ENDPOINT"
ALLOY_USERNAME_ENV = "ARCHIVE_OTLP_USERNAME"
ALLOY_PASSWORD_ENV = "ARCHIVE_OTLP_PASSWORD"  # noqa: S105  # pragma: allowlist secret

# Where cloud-init puts the archive leg on an EC2 host. Vector loads every file
# in its config directory, so the sink lives beside the image's templates
# without an AMI rebuild. Alloy imports the module from a fixed path that the
# image pre-populates with a discarding sink (bilder/components/alloy).
EC2_VECTOR_SINK_PATH = "/etc/vector/archive_logs.yaml"
EC2_ALLOY_MODULE_PATH = "/etc/alloy/archive.alloy"

# The archive is best effort (ADR-0014): a stalled archive must never hold up
# the Grafana Cloud stream that shares the writer. A bounded memory buffer that
# drops the newest events and a short retry budget are what make the Vector
# sink non-blocking.
VECTOR_SINK_BUFFER_MAX_EVENTS = 5000
VECTOR_SINK_RETRY_ATTEMPTS = 3

# Which transforms each EC2 image's Vector log pipeline ends in, and the labels
# its Grafana Cloud sink applies. The archive sink reuses both so the two
# backends hold the same streams. tests/ol_infrastructure/lib/
# test_telemetry_archive.py checks these against the image templates.
EC2_VECTOR_LOG_PIPELINES: dict[str, dict[str, Any]] = {
    "concourse": {
        "inputs": ["enrich_concourse_logs"],
        "labels": {
            "environment": "${ENVIRONMENT}",
            "application": "concourse",
            "service": "concourse",
        },
    },
    "consul": {
        "inputs": ["filter_cron_consul_auth_logs", "enrich_consul_journald_logs"],
        "labels": {
            "environment": "${ENVIRONMENT}",
            "application": "consul",
            "service": "consul",
            "hostname": "${HOSTNAME}",
        },
    },
    "vault": {
        "inputs": ["filter_cron_vault_auth_logs", "enrich_vault_journald_logs"],
        "labels": {
            "environment": "${ENVIRONMENT}",
            "application": "vault",
            "service": "vault",
            "hostname": "${HOSTNAME}",
        },
    },
}


@dataclass(frozen=True)
class ArchivePushTarget:
    """The write endpoint and its basic-auth credential for one environment."""

    domain: str
    username: str
    password: str

    @property
    def base_url(self) -> str:
        """The endpoint a Vector loki sink takes; Vector appends the push path."""
        return f"https://{self.domain}"

    @property
    def otlp_url(self) -> str:
        """The endpoint an OTLP/HTTP exporter takes; it appends /v1/<signal>."""
        return f"{self.base_url}{OTLP_BASE_PATH}"

    @property
    def loki_push_url(self) -> str:
        """The full Loki push URL, for clients that take one."""
        return f"{self.base_url}{LOKI_PUSH_PATH}"


def archive_domain(env_suffix: str) -> str | None:
    """Return the archive hostname for an environment, or None if it has none."""
    return ARCHIVE_DOMAINS.get(env_suffix)


def push_target(env_suffix: str) -> ArchivePushTarget | None:
    """Read the archive endpoint and push credential for an environment.

    The credential is the one the archive stack seeds into Vault for its
    Traefik basic-auth middleware, read from the same SOPS file so both sides
    always hold the same secret.
    """
    domain = archive_domain(env_suffix)
    if domain is None:
        return None
    secrets = read_yaml_secrets(
        Path(f"observability_archive/archive.{env_suffix}.yaml")
    )
    return ArchivePushTarget(
        domain=domain,
        username=secrets["push_username"],
        password=secrets["push_password"],
    )


def vector_loki_sink(
    *,
    inputs: list[str],
    labels: dict[str, str],
    target: ArchivePushTarget,
    user_env: str = VECTOR_USER_ENV,
    password_env: str = VECTOR_PASSWORD_ENV,
) -> dict[str, Any]:
    """Build a Vector `loki` sink to the archive that cannot block its inputs."""
    return {
        "type": "loki",
        "inputs": inputs,
        "endpoint": target.base_url,
        "auth": {
            "strategy": "basic",
            "user": f"${{{user_env}}}",
            "password": f"${{{password_env}}}",
        },
        "encoding": {"codec": "json"},
        # The EC2 unit's ExecStartPre `vector validate` runs sink healthchecks;
        # an unreachable archive must not keep Vector from starting.
        "healthcheck": False,
        "labels": labels,
        "out_of_order_action": "rewrite_timestamp",
        "buffer": {
            "type": "memory",
            "max_events": VECTOR_SINK_BUFFER_MAX_EVENTS,
            "when_full": "drop_newest",
        },
        "request": {"retry_attempts": VECTOR_SINK_RETRY_ATTEMPTS},
    }


def ec2_vector_sink_file(image: str, target: ArchivePushTarget) -> dict[str, str]:
    """Build the cloud-init `write_files` entry that adds the archive sink to a host."""
    pipeline = EC2_VECTOR_LOG_PIPELINES[image]
    sink = vector_loki_sink(
        inputs=pipeline["inputs"], labels=pipeline["labels"], target=target
    )
    return {
        "path": EC2_VECTOR_SINK_PATH,
        "content": yaml.safe_dump(
            {"sinks": {f"ship_{image}_logs_to_archive": sink}}, sort_keys=False
        ),
        "owner": "vector:vector",
        "permissions": "0644",
    }


def ec2_vector_env_lines(target: ArchivePushTarget) -> str:
    """Lines for /etc/default/vector carrying the archive credential."""
    return (
        f"{VECTOR_USER_ENV}={target.username}\n"
        f"{VECTOR_PASSWORD_ENV}={target.password}\n"
    )


def ec2_alloy_env_lines(target: ArchivePushTarget) -> str:
    """Lines for /etc/default/alloy carrying the archive endpoint and credential."""
    return (
        f'{ALLOY_ENDPOINT_ENV}="{target.otlp_url}"\n'
        f'{ALLOY_USERNAME_ENV}="{target.username}"\n'
        f'{ALLOY_PASSWORD_ENV}="{target.password}"\n'
    )


# The module bilder/components/alloy/files/config.alloy imports. The image's
# own copy of this file discards; this one exports a non-blocking OTLP/HTTP
# exporter. A second loki.write would not do: its wal and queue_config blocks
# are experimental in Alloy, and without them a stalled archive writer
# back-pressures the shared otelcol.exporter.loki and delays Cloud delivery
# (plan D10).
EC2_ALLOY_ARCHIVE_MODULE = """\
// Written by cloud-init from ol_infrastructure.lib.telemetry_archive.
// Archive leg of the log pipeline: non-blocking, short retry budget, so a
// stalled archive cannot delay delivery to Grafana Cloud.
declare "log_sink" {
  export "input" {
    value = otelcol.exporter.otlphttp.archive.input
  }

  otelcol.exporter.otlphttp "archive" {
    client {
      endpoint = sys.env("ARCHIVE_OTLP_ENDPOINT")
      auth     = otelcol.auth.basic.archive.handler
    }

    retry_on_failure {
      max_elapsed_time = "1m"
    }

    sending_queue {
      block_on_overflow = false
      queue_size        = 200
    }
  }

  otelcol.auth.basic "archive" {
    username = sys.env("ARCHIVE_OTLP_USERNAME")
    password = sys.env("ARCHIVE_OTLP_PASSWORD")
  }
}
"""


def ec2_alloy_module_file() -> dict[str, str]:
    """Build the cloud-init `write_files` entry that replaces the image's null sink."""
    return {
        "path": EC2_ALLOY_MODULE_PATH,
        "content": EC2_ALLOY_ARCHIVE_MODULE,
        "owner": "alloy:alloy",
        "permissions": "0644",
    }
