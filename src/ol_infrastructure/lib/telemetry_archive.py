"""What every writer to the telemetry archive (ADR-0014) has to agree on.

The archive stack (`applications/observability_archive`) builds the endpoint;
the k8s-monitoring collectors, vector-log-proxy and the EC2 hosts running
Vector and Alloy write to it. This module is the one place the hostname, the
push paths, the credential file and the shape of a bounded Vector sink are
spelled out, so a writer cannot drift from the endpoint it ships to. One
credential covers every push path: the archive's Traefik authenticates the
request, and the backend behind each path never sees it.

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

# Loki's native push API and its OTLP receiver, Mimir's remote-write API and
# its OTLP receiver. OTLP clients append /v1/<signal> themselves, so they are
# given the /otlp prefix and the archive routes both signal paths.
LOKI_PUSH_PATH = "/loki/api/v1/push"
PROMETHEUS_PUSH_PATH = "/api/v1/push"
OTLP_LOGS_PATH = "/otlp/v1/logs"
OTLP_METRICS_PATH = "/otlp/v1/metrics"
OTLP_BASE_PATH = "/otlp"

# Grafana Cloud datasource UIDs for the archive. Fixed (plan D9) so a dashboard
# or a test can name them; never valid in an alert rule (D13).
ARCHIVE_LOKI_DATASOURCE_UID = "archive-loki"
ARCHIVE_MIMIR_DATASOURCE_UID = "archive-mimir"

# The archive namespace on the operations cluster, and how its recording
# rules reach Mimir's ruler: the operations cluster's alloy-metrics syncs
# PrometheusRule objects carrying this label from this namespace to the
# ruler's in-cluster Service (the archive ingress routes push paths only).
ARCHIVE_NAMESPACE = "observability-archive"
MIMIR_RULES_LABEL = {"ol.mit.edu/mimir-rules": "true"}
MIMIR_RULER_URL = f"http://mimir-ruler.{ARCHIVE_NAMESPACE}.svc.cluster.local:8080"

# Environment variables the EC2 Vector and Alloy configs read. The same push
# credential authenticates the log and the metric legs.
PUSH_USERNAME_ENV = "ARCHIVE_PUSH_USERNAME"
PUSH_PASSWORD_ENV = "ARCHIVE_PUSH_PASSWORD"  # noqa: S105  # pragma: allowlist secret
ALLOY_OTLP_ENDPOINT_ENV = "ARCHIVE_OTLP_ENDPOINT"
ALLOY_REMOTE_WRITE_ENDPOINT_ENV = "ARCHIVE_REMOTE_WRITE_ENDPOINT"

# Where cloud-init puts the archive legs on an EC2 host. Vector loads every
# file in its config directory, so the sinks live beside the image's templates
# without an AMI rebuild. Alloy imports the module from a fixed path that the
# image pre-populates with discarding sinks (bilder/components/alloy).
EC2_VECTOR_LOG_SINK_PATH = "/etc/vector/archive_logs.yaml"
EC2_VECTOR_METRIC_SINK_PATH = "/etc/vector/archive_metrics.yaml"
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

# The transform each EC2 Vector metric pipeline ends in, and which hosts run
# it. Host metrics come from components/vector/templates/host_metrics.yaml on
# every image; Concourse web nodes add the exporter scrape in
# images/concourse/templates/vector/concourse_metrics.yaml. Keyed by host role
# rather than image because the two Concourse roles share an image build but
# only web installs the second template, and a sink naming an input the host
# does not have fails Vector's config load.
HOST_METRICS_INPUT = "add_labels_to_host_metrics"
CONCOURSE_METRICS_INPUT = "add_labels_to_concourse_metrics"
EC2_VECTOR_METRIC_INPUTS: dict[str, list[str]] = {
    "concourse-web": [HOST_METRICS_INPUT, CONCOURSE_METRICS_INPUT],
    "concourse-worker": [HOST_METRICS_INPUT],
    "consul": [HOST_METRICS_INPUT],
    "vault": [HOST_METRICS_INPUT],
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

    @property
    def remote_write_url(self) -> str:
        """The full Prometheus remote-write URL into Mimir."""
        return f"{self.base_url}{PROMETHEUS_PUSH_PATH}"


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


def _bounded_vector_sink(
    sink_type: str,
    *,
    inputs: list[str],
    endpoint: str,
    user_env: str,
    password_env: str,
) -> dict[str, Any]:
    return {
        "type": sink_type,
        "inputs": inputs,
        "endpoint": endpoint,
        "auth": {
            "strategy": "basic",
            "user": f"${{{user_env}}}",
            "password": f"${{{password_env}}}",
        },
        # The EC2 unit's ExecStartPre `vector validate` runs sink healthchecks;
        # an unreachable archive must not keep Vector from starting.
        "healthcheck": False,
        "buffer": {
            "type": "memory",
            "max_events": VECTOR_SINK_BUFFER_MAX_EVENTS,
            "when_full": "drop_newest",
        },
        "request": {"retry_attempts": VECTOR_SINK_RETRY_ATTEMPTS},
    }


def vector_loki_sink(
    *,
    inputs: list[str],
    labels: dict[str, str],
    target: ArchivePushTarget,
    user_env: str = PUSH_USERNAME_ENV,
    password_env: str = PUSH_PASSWORD_ENV,
) -> dict[str, Any]:
    """Build a Vector `loki` sink to the archive that cannot block its inputs."""
    return {
        **_bounded_vector_sink(
            "loki",
            inputs=inputs,
            endpoint=target.base_url,
            user_env=user_env,
            password_env=password_env,
        ),
        "encoding": {"codec": "json"},
        "labels": labels,
        "out_of_order_action": "rewrite_timestamp",
    }


def vector_remote_write_sink(
    *,
    inputs: list[str],
    target: ArchivePushTarget,
    user_env: str = PUSH_USERNAME_ENV,
    password_env: str = PUSH_PASSWORD_ENV,
) -> dict[str, Any]:
    """Build a non-blocking Vector `prometheus_remote_write` sink to the archive."""
    return _bounded_vector_sink(
        "prometheus_remote_write",
        inputs=inputs,
        endpoint=target.remote_write_url,
        user_env=user_env,
        password_env=password_env,
    )


def _vector_sink_file(path: str, sinks: dict[str, Any]) -> dict[str, str]:
    return {
        "path": path,
        "content": yaml.safe_dump({"sinks": sinks}, sort_keys=False),
        "owner": "vector:vector",
        "permissions": "0644",
    }


def ec2_vector_sink_file(image: str, target: ArchivePushTarget) -> dict[str, str]:
    """Build the cloud-init `write_files` entry for a host's archive log sink."""
    pipeline = EC2_VECTOR_LOG_PIPELINES[image]
    sink = vector_loki_sink(
        inputs=pipeline["inputs"], labels=pipeline["labels"], target=target
    )
    return _vector_sink_file(
        EC2_VECTOR_LOG_SINK_PATH, {f"ship_{image}_logs_to_archive": sink}
    )


def ec2_vector_metric_sink_file(role: str, target: ArchivePushTarget) -> dict[str, str]:
    """Build the cloud-init `write_files` entry for a host's archive metric sink."""
    sink = vector_remote_write_sink(
        inputs=EC2_VECTOR_METRIC_INPUTS[role], target=target
    )
    return _vector_sink_file(
        EC2_VECTOR_METRIC_SINK_PATH, {"ship_metrics_to_archive": sink}
    )


def ec2_vector_env_lines(target: ArchivePushTarget) -> str:
    """Lines for /etc/default/vector carrying the archive credential."""
    return (
        f"{PUSH_USERNAME_ENV}={target.username}\n"
        f"{PUSH_PASSWORD_ENV}={target.password}\n"
    )


def ec2_alloy_env_lines(target: ArchivePushTarget) -> str:
    """Lines for /etc/default/alloy carrying the archive endpoints and credential."""
    return (
        f'{ALLOY_OTLP_ENDPOINT_ENV}="{target.otlp_url}"\n'
        f'{ALLOY_REMOTE_WRITE_ENDPOINT_ENV}="{target.remote_write_url}"\n'
        f'{PUSH_USERNAME_ENV}="{target.username}"\n'
        f'{PUSH_PASSWORD_ENV}="{target.password}"\n'
    )


# The module bilder/components/alloy/files/config.alloy imports. The image's
# own copy of this file discards; this one exports a non-blocking OTLP/HTTP
# exporter for logs and a remote-write with a capped sample age for metrics.
# A second loki.write would not do for logs: its wal and queue_config blocks
# are experimental in Alloy, and without them a stalled archive writer
# back-pressures the shared otelcol.exporter.loki and delays Cloud delivery
# (plan D10). prometheus.remote_write's queue_config and wal are generally
# available, and sample_age_limit is what keeps a long outage from replaying
# hours of stale samples when the archive comes back. Two declares rather than
# one with two exports so an image built before Phase 2 (importing only
# log_sink) still loads this file.
EC2_ALLOY_ARCHIVE_MODULE = """\
// Written by cloud-init from ol_infrastructure.lib.telemetry_archive.
// Archive legs of the pipeline: non-blocking, short retry budgets, so a
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
    username = sys.env("ARCHIVE_PUSH_USERNAME")
    password = sys.env("ARCHIVE_PUSH_PASSWORD")
  }
}

declare "metric_sink" {
  export "receiver" {
    value = prometheus.remote_write.archive.receiver
  }

  prometheus.remote_write "archive" {
    endpoint {
      url = sys.env("ARCHIVE_REMOTE_WRITE_ENDPOINT")

      basic_auth {
        username = sys.env("ARCHIVE_PUSH_USERNAME")
        password = sys.env("ARCHIVE_PUSH_PASSWORD")
      }

      queue_config {
        max_shards         = 4
        retry_on_http_429  = false
        sample_age_limit   = "1h"
      }
    }

    wal {
      max_keepalive_time = "2h"
    }
  }
}
"""


def ec2_alloy_module_file() -> dict[str, str]:
    """Build the cloud-init `write_files` entry that replaces the image's null sinks."""
    return {
        "path": EC2_ALLOY_MODULE_PATH,
        "content": EC2_ALLOY_ARCHIVE_MODULE,
        "owner": "alloy:alloy",
        "permissions": "0644",
    }
