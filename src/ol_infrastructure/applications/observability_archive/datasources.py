"""Grafana Cloud datasources that read the archive over PDC (plan D8, D9).

Declared here, beside the PDC agent that carries their queries, so the
in-cluster URLs and the network id come from the same program. The UIDs are
fixed so dashboards can name them and the D13 test can refuse them in alert
rules.
"""

import json

import pulumiverse_grafana as grafana
from pulumi import ResourceOptions

from ol_infrastructure.applications.observability_archive.pdc import (
    PdcReadPath,
    query_frontend_hosts,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo
from ol_infrastructure.lib.telemetry_archive import (
    ARCHIVE_LOKI_DATASOURCE_UID,
    ARCHIVE_MIMIR_DATASOURCE_UID,
)

DAYS_PER_YEAR = 365


def retention_label(retention_days: int) -> str:
    """How the datasource name states its retention: `13mo` or `30d`."""
    if retention_days >= DAYS_PER_YEAR:
        return f"{round(retention_days / 30)}mo"
    return f"{retention_days}d"


def loki_datasource(
    *,
    stack_info: StackInfo,
    namespace: str,
    retention_days: int,
    read_path: PdcReadPath,
) -> grafana.oss.DataSource:
    """`Loki (archive, …)`: the archive's Loki query-frontend, reached over PDC."""
    host = query_frontend_hosts(namespace)["loki"]
    return grafana.oss.DataSource(
        f"telemetry-archive-loki-datasource-{stack_info.env_suffix}",
        type="loki",
        name=f"Loki (archive, {retention_label(retention_days)})",
        uid=ARCHIVE_LOKI_DATASOURCE_UID,
        url=f"http://{host}",
        access_mode="proxy",
        private_data_source_connect_network_id=read_path.network.pdc_network_id,
        json_data_encoded=json.dumps(
            {
                "maxLines": 1000,
                # Long ranges are the point of this datasource. Loki splits
                # them by split_queries_by_interval server side.
                "timeout": 300,
                # Keeps the archive out of the Alerting UI's data
                # source-managed rules. It does not stop a Grafana-managed
                # rule from querying this datasource (grafana/grafana#132468);
                # plan D13 is enforced for rules declared in this repo by
                # test_no_archive_datasource_in_alert_rules.py and otherwise
                # by convention.
                "manageAlerts": False,
            }
        ),
        opts=ResourceOptions(provider=read_path.provider),
    )


def mimir_datasource(
    *,
    stack_info: StackInfo,
    namespace: str,
    retention_days: int,
    read_path: PdcReadPath,
) -> grafana.oss.DataSource:
    """`Mimir (archive, …)`: the archive's Mimir query-frontend, reached over PDC."""
    host = query_frontend_hosts(namespace)["mimir"]
    return grafana.oss.DataSource(
        f"telemetry-archive-mimir-datasource-{stack_info.env_suffix}",
        type="prometheus",
        name=f"Mimir (archive, {retention_label(retention_days)})",
        uid=ARCHIVE_MIMIR_DATASOURCE_UID,
        url=f"http://{host}/prometheus",
        access_mode="proxy",
        private_data_source_connect_network_id=read_path.network.pdc_network_id,
        json_data_encoded=json.dumps(
            {
                "httpMethod": "POST",
                "prometheusType": "Mimir",
                "prometheusVersion": "2.9.1",
                # The collectors' scrape interval, so $__rate_interval is
                # never shorter than two samples.
                "timeInterval": "60s",
                "queryTimeout": "300s",
                # The archive's ruler exists for recording_rules.py, not for
                # rules written in the Alerting UI; and Grafana-managed
                # recording rules must not write into the archive. Neither
                # setting stops a Grafana-managed alert rule from querying
                # this datasource (see loki_datasource).
                "manageAlerts": False,
                "allowAsRecordingRulesTarget": False,
            }
        ),
        opts=ResourceOptions(provider=read_path.provider),
    )
