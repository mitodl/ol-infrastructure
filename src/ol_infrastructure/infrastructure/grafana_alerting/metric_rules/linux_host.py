"""Linux host alert rules (CPU, memory, disk).

Source: grafana-alerts/cortex-rules/linux-host.yaml

These rules have no cluster label filter — they apply to all EC2 hosts
regardless of environment and fire on whichever stack scrapes them.
Three separate groups to preserve the original evaluation intervals.
"""

from collections.abc import Callable

from pulumi import Input, ResourceOptions
from pulumiverse_grafana import alerting


def create(
    folder_uid: Input[str],
    rd: Callable[[str], list[alerting.RuleGroupRuleDataArgs]],
    resource_opts: ResourceOptions,
) -> None:
    """Create Linux host alert rule groups (CPU, memory, disk)."""
    # CPU usage > 80% sustained for 6 hours (warning only — no critical rule).
    alerting.RuleGroup(
        "linux-host-cpu-usage",
        name="cpu-usage",
        folder_uid=folder_uid,
        interval_seconds=3600,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="CPUUsageWarning",
                condition="C",
                for_="6h",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": 'CPU usage on {{ $labels.instance }} has been at {{ printf "%.2f" $value }} for at least 6 hours.'
                },
                datas=rd(
                    "1 - (\n"
                    '  sum by (cluster, instance) (rate(host_cpu_seconds_total{mode="idle", job="integrations/linux_host"}[5m]))\n'
                    '  / sum by (cluster, instance) (rate(host_cpu_seconds_total{job="integrations/linux_host"}[5m]))\n'
                    ") > 0.8"
                ),
            ),
        ],
        opts=resource_opts,
    )

    # Memory usage > 90% sustained for 2 hours (warning only).
    alerting.RuleGroup(
        "linux-host-memory-usage",
        name="memory-usage",
        folder_uid=folder_uid,
        interval_seconds=1800,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="MemoryUsageWarning",
                condition="C",
                for_="2h",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": 'Memory usage on {{ $labels.instance }} has been at {{ printf "%.2f" $value }} for at least 2 hours.'
                },
                datas=rd("(host_memory_used_bytes / host_memory_total_bytes) > 0.9"),
            ),
        ],
        opts=resource_opts,
    )

    # Disk usage thresholds: warning > 80% for 1h, critical > 95% for 10m.
    # Excludes pseudo-filesystems (squashfs, vfat) and non-/dev/ devices.
    alerting.RuleGroup(
        "linux-host-disk-usage",
        name="disk-usage",
        folder_uid=folder_uid,
        interval_seconds=600,
        rules=[
            alerting.RuleGroupRuleArgs(
                name="DiskUsageWarning",
                condition="C",
                for_="1h",
                no_data_state="OK",
                labels={"severity": "warning"},
                annotations={
                    "description": 'Filesystem on {{ $labels.device }} at {{ $labels.instance }} is {{ printf "%.2f" $value }}% full.'
                },
                datas=rd(
                    '(host_filesystem_used_ratio{device=~"/dev.*",filesystem!~"(squashfs|vfat)",job="integrations/linux_host"} * 100) > 80'
                ),
            ),
            alerting.RuleGroupRuleArgs(
                name="DiskUsageCritical",
                condition="C",
                for_="10m",
                no_data_state="OK",
                labels={"severity": "critical"},
                annotations={
                    "description": 'Filesystem on {{ $labels.device }} at {{ $labels.instance }} is {{ printf "%.2f" $value }}% full.'
                },
                datas=rd(
                    '(host_filesystem_used_ratio{device=~"/dev.*",filesystem!~"(squashfs|vfat)",job="integrations/linux_host"} * 100) > 95'
                ),
            ),
        ],
        opts=resource_opts,
    )
