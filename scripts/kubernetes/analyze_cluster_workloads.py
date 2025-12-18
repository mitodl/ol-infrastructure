#!/usr/bin/env python3
"""Analyze Kubernetes workload resource usage versus requests/limits.

This script:
1. Uses `kubectl top` (metrics-server required) to capture live CPU/Memory usage
2. Compares usage against configured requests and limits for each workload
3. Highlights under/over-provisioned workloads with actionable suggestions
4. Supports optional namespace scoping
"""

import json
import re
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

import cyclopts
from kubernetes import client, config
from kubernetes.client import ApiException

# Constants for resource conversion
MEMORY_UNITS = {
    "Ki": Decimal("1024"),
    "Mi": Decimal("1024") ** 2,
    "Gi": Decimal("1024") ** 3,
    "K": Decimal("1000"),
    "M": Decimal("1000") ** 2,
    "G": Decimal("1000") ** 3,
}

CPU_LOW_UTILIZATION = Decimal("0.3")  # below this, requests likely too high
CPU_HIGH_UTILIZATION = Decimal("0.9")  # above this, requests likely too low
LIMIT_PRESSURE = Decimal("0.8")  # nearing limits
MEMORY_LOW_UTILIZATION = Decimal("0.4")
MEMORY_HIGH_UTILIZATION = Decimal("0.9")

NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class WorkloadType(Enum):
    """Kubernetes workload types."""

    DEPLOYMENT = "Deployment"
    STATEFULSET = "StatefulSet"
    DAEMONSET = "DaemonSet"
    JOB = "Job"
    CRONJOB = "CronJob"
    REPLICASET = "ReplicaSet"
    POD = "Pod"
    UNKNOWN = "Unknown"


@dataclass
class ResourceRequest:
    """Resource request/limit for a container."""

    cpu_cores: Decimal = Decimal("0")
    memory_bytes: Decimal = Decimal("0")

    def __add__(self, other: "ResourceRequest") -> "ResourceRequest":
        """Add two resource requests."""
        return ResourceRequest(
            cpu_cores=self.cpu_cores + other.cpu_cores,
            memory_bytes=self.memory_bytes + other.memory_bytes,
        )

    def __radd__(self, other: Any) -> "ResourceRequest":
        """Support sum() function."""
        if other == 0:
            return ResourceRequest()
        return self.__add__(other)

    def to_human_readable(self) -> dict[str, str]:
        """Convert to human-readable format."""
        return {
            "cpu_cores": f"{float(self.cpu_cores):.3f}",
            "cpu_millicores": f"{int(self.cpu_cores * 1000)}m",
            "memory_bytes": f"{int(self.memory_bytes)}",
            "memory_gb": f"{float(self.memory_bytes / (1024**3)):.2f}GB",
        }


@dataclass
class WorkloadUsage:
    """Aggregated usage and limits for a workload."""

    name: str
    namespace: str
    kind: str
    requests: ResourceRequest = field(default_factory=ResourceRequest)
    limits: ResourceRequest = field(default_factory=ResourceRequest)
    usage: ResourceRequest = field(default_factory=ResourceRequest)
    pods: set[str] = field(default_factory=set)
    missing_metrics: int = 0

    def utilization(self) -> dict[str, float]:
        """Return utilization ratios for CPU and memory."""
        cpu_ratio = (
            float(self.usage.cpu_cores / self.requests.cpu_cores)
            if self.requests.cpu_cores > 0
            else 0.0
        )
        mem_ratio = (
            float(self.usage.memory_bytes / self.requests.memory_bytes)
            if self.requests.memory_bytes > 0
            else 0.0
        )
        return {"cpu": cpu_ratio, "memory": mem_ratio}


@dataclass
class Suggestion:
    """Recommendation for a workload."""

    workload: str
    namespace: str
    kind: str
    severity: str
    message: str


def parse_cpu_quantity(cpu_str: str | None) -> Decimal:
    """Parse CPU quantity to cores."""
    if not cpu_str:
        return Decimal("0")

    cpu_str = cpu_str.strip()
    if cpu_str.endswith("m"):
        return Decimal(cpu_str[:-1]) / Decimal("1000")
    try:
        return Decimal(cpu_str)
    except InvalidOperation:
        return Decimal("0")


def parse_memory_quantity(mem_str: str | None) -> Decimal:
    """Parse memory quantity to bytes."""
    if not mem_str:
        return Decimal("0")

    mem_str = mem_str.strip()

    for unit, multiplier in sorted(MEMORY_UNITS.items(), key=lambda x: -len(x[0])):
        if mem_str.endswith(unit):
            try:
                value = Decimal(mem_str[: -len(unit)])
            except InvalidOperation:
                print(f"Invalid memory quantity '{mem_str}'", file=sys.stderr)
                break
            return value * multiplier

    return Decimal("0")


def format_cpu(cores: Decimal) -> str:
    """Format CPU cores to millicores string."""
    return f"{int((cores * 1000).to_integral_value())}m"


def format_memory(bytes_val: Decimal) -> str:
    """Format memory bytes to Mi string."""
    return f"{int((bytes_val / (Decimal('1024') ** 2)).to_integral_value())}Mi"


def run_kubectl_top(
    namespace: str | None,
) -> dict[tuple[str, str, str], ResourceRequest]:
    """Run `kubectl top` and return usage keyed by (namespace, pod, container)."""
    if namespace and not NAMESPACE_PATTERN.fullmatch(namespace):
        msg = f"Invalid namespace value: {namespace}"
        raise ValueError(msg)

    cmd = ["kubectl", "top", "pod", "--containers", "--no-headers"]
    if namespace:
        cmd.extend(["-n", namespace])
    else:
        cmd.append("-A")

    try:
        result = subprocess.run(  # noqa: S603 input validated and shell disabled
            cmd,
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ) as exc:
        print(f"Error running {' '.join(cmd)}: {exc}", file=sys.stderr)
        return {}

    metrics: dict[tuple[str, str, str], ResourceRequest] = {}
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) == 5:
            ns, pod, container, cpu_str, mem_str = parts
        elif len(parts) == 4 and namespace:
            pod, container, cpu_str, mem_str = parts
            ns = namespace
        else:
            continue

        metrics[(ns, pod, container)] = ResourceRequest(
            cpu_cores=parse_cpu_quantity(cpu_str),
            memory_bytes=parse_memory_quantity(mem_str),
        )
    return metrics


def resolve_workload(pod: client.V1Pod, apps_api: client.AppsV1Api) -> tuple[str, str]:
    """Resolve the top-level workload for a pod."""
    owner_refs = pod.metadata.owner_references or []
    if not owner_refs:
        return (WorkloadType.POD.value, pod.metadata.name)

    owner = owner_refs[0]
    if owner.kind == WorkloadType.REPLICASET.value:
        try:
            rs = apps_api.read_namespaced_replica_set(
                owner.name, pod.metadata.namespace
            )
            rs_owner_refs = rs.metadata.owner_references or []
            for rs_owner in rs_owner_refs:
                if rs_owner.kind == WorkloadType.DEPLOYMENT.value:
                    return (rs_owner.kind, rs_owner.name)
        except ApiException:
            return (owner.kind, owner.name)
        else:
            return (owner.kind, owner.name)

    if owner.kind in {
        WorkloadType.DEPLOYMENT.value,
        WorkloadType.STATEFULSET.value,
        WorkloadType.DAEMONSET.value,
        WorkloadType.JOB.value,
        WorkloadType.CRONJOB.value,
    }:
        return (owner.kind, owner.name)

    return (owner.kind or WorkloadType.UNKNOWN.value, owner.name)


def collect_workload_usage(
    namespace: str | None,
) -> tuple[list[WorkloadUsage], int]:
    """Collect workload usage, requests, and limits."""
    metrics = run_kubectl_top(namespace)
    apps_api = client.AppsV1Api()
    core_api = client.CoreV1Api()

    if namespace:
        pods = core_api.list_namespaced_pod(namespace=namespace)
    else:
        pods = core_api.list_pod_for_all_namespaces()

    workloads: dict[tuple[str, str, str], WorkloadUsage] = {}
    missing_metrics = 0

    for pod in pods.items:
        if pod.status.phase in {"Succeeded", "Failed"}:
            continue
        workload_kind, workload_name = resolve_workload(pod, apps_api)
        key = (pod.metadata.namespace, workload_name, workload_kind)

        if key not in workloads:
            workloads[key] = WorkloadUsage(
                name=workload_name,
                namespace=pod.metadata.namespace,
                kind=workload_kind,
            )

        wl = workloads[key]
        wl.pods.add(pod.metadata.name)

        for container in pod.spec.containers:
            requests = container.resources.requests or {}
            limits = container.resources.limits or {}
            container_requests = ResourceRequest(
                cpu_cores=parse_cpu_quantity(requests.get("cpu")),
                memory_bytes=parse_memory_quantity(requests.get("memory")),
            )
            container_limits = ResourceRequest(
                cpu_cores=parse_cpu_quantity(limits.get("cpu")),
                memory_bytes=parse_memory_quantity(limits.get("memory")),
            )
            usage = metrics.get(
                (pod.metadata.namespace, pod.metadata.name, container.name)
            )
            if usage is None:
                wl.missing_metrics += 1
                missing_metrics += 1
                usage = ResourceRequest()

            wl.requests += container_requests
            wl.limits += container_limits
            wl.usage += usage

    return list(workloads.values()), missing_metrics


def suggest_adjustments(workloads: Iterable[WorkloadUsage]) -> list[Suggestion]:
    """Generate suggestions based on utilization."""
    suggestions: list[Suggestion] = []

    for wl in workloads:
        cpu_req = wl.requests.cpu_cores
        cpu_limit = wl.limits.cpu_cores
        cpu_use = wl.usage.cpu_cores
        mem_req = wl.requests.memory_bytes
        mem_limit = wl.limits.memory_bytes
        mem_use = wl.usage.memory_bytes

        def add_suggestion(severity: str, message: str) -> None:
            suggestions.append(
                Suggestion(
                    workload=wl.name,
                    namespace=wl.namespace,
                    kind=wl.kind,
                    severity=severity,
                    message=message,
                )
            )

        if cpu_req == 0 and cpu_use > Decimal("0.05"):
            target = cpu_use * Decimal("1.2")
            add_suggestion(
                "warn",
                f"Set CPU requests to ~{format_cpu(target)} (currently none; usage {format_cpu(cpu_use)}).",
            )
        elif cpu_req > 0:
            cpu_ratio = cpu_use / cpu_req
            if cpu_ratio < CPU_LOW_UTILIZATION:
                target = max(cpu_use * Decimal("1.2"), Decimal("0.05"))
                add_suggestion(
                    "info",
                    f"CPU under-utilized ({float(cpu_ratio * 100):.1f}% of request). Consider lowering request to ~{format_cpu(target)}.",
                )
            elif cpu_ratio > CPU_HIGH_UTILIZATION:
                target = cpu_use * Decimal("1.2")
                add_suggestion(
                    "warn",
                    f"CPU near capacity ({float(cpu_ratio * 100):.1f}% of request). Consider raising request to ~{format_cpu(target)}.",
                )

        if cpu_limit > 0:
            limit_ratio = cpu_use / cpu_limit
            if limit_ratio > LIMIT_PRESSURE:
                add_suggestion(
                    "warn",
                    f"CPU usage at {float(limit_ratio * 100):.1f}% of limit ({format_cpu(cpu_use)} used). Consider increasing limit or optimizing.",
                )

        if mem_req == 0 and mem_use > Decimal("50") * MEMORY_UNITS["Mi"]:
            target = mem_use * Decimal("1.2")
            add_suggestion(
                "warn",
                f"Set memory requests to ~{format_memory(target)} (currently none; usage {format_memory(mem_use)}).",
            )
        elif mem_req > 0:
            mem_ratio = mem_use / mem_req
            if mem_ratio < MEMORY_LOW_UTILIZATION:
                target = max(
                    mem_use * Decimal("1.2"), MEMORY_UNITS["Mi"] * Decimal("50")
                )
                add_suggestion(
                    "info",
                    f"Memory under-utilized ({float(mem_ratio * 100):.1f}% of request). Consider lowering request to ~{format_memory(target)}.",
                )
            elif mem_ratio > MEMORY_HIGH_UTILIZATION:
                target = mem_use * Decimal("1.2")
                add_suggestion(
                    "warn",
                    f"Memory near capacity ({float(mem_ratio * 100):.1f}% of request). Consider raising request to ~{format_memory(target)}.",
                )

        if mem_limit > 0:
            limit_ratio = mem_use / mem_limit
            if limit_ratio > LIMIT_PRESSURE:
                add_suggestion(
                    "warn",
                    f"Memory usage at {float(limit_ratio * 100):.1f}% of limit ({format_memory(mem_use)} used). Consider increasing limit or optimizing.",
                )

    return suggestions


def print_report(
    workloads: list[WorkloadUsage],
    suggestions: list[Suggestion],
    missing_metrics: int,
    namespace: str | None,
) -> None:
    """Print a human-readable report."""
    scope = namespace or "all namespaces"
    print("\n" + "=" * 80)
    print(f"KUBERNETES WORKLOAD UTILIZATION ANALYSIS ({scope})")
    print("=" * 80)

    total_usage = sum((wl.usage for wl in workloads), ResourceRequest())
    total_requests = sum((wl.requests for wl in workloads), ResourceRequest())
    total_limits = sum((wl.limits for wl in workloads), ResourceRequest())

    print("\n### AGGREGATED TOTALS ###\n")
    print("Usage:")
    for key, value in total_usage.to_human_readable().items():
        print(f"  {key}: {value}")
    print("\nRequests:")
    for key, value in total_requests.to_human_readable().items():
        print(f"  {key}: {value}")
    print("\nLimits:")
    for key, value in total_limits.to_human_readable().items():
        print(f"  {key}: {value}")

    if missing_metrics:
        print(
            f"\n⚠️  Missing metrics for {missing_metrics} container(s); results may be incomplete.",
            file=sys.stderr,
        )

    if not workloads:
        print("\nNo workloads found.")
        return

    cpu_sorted = sorted(
        workloads,
        key=lambda wl: (
            wl.requests.cpu_cores > 0,
            wl.usage.cpu_cores / wl.requests.cpu_cores
            if wl.requests.cpu_cores > 0
            else Decimal("0"),
        ),
        reverse=True,
    )
    mem_sorted = sorted(
        workloads,
        key=lambda wl: (
            wl.requests.memory_bytes > 0,
            wl.usage.memory_bytes / wl.requests.memory_bytes
            if wl.requests.memory_bytes > 0
            else Decimal("0"),
        ),
        reverse=True,
    )

    print("\n### TOP CPU UTILIZATION (vs requests) ###\n")
    for wl in cpu_sorted[:10]:
        cpu_ratio = (
            wl.usage.cpu_cores / wl.requests.cpu_cores
            if wl.requests.cpu_cores > 0
            else Decimal("0")
        )
        print(
            f"{wl.namespace}/{wl.name} ({wl.kind}): "
            f"usage {format_cpu(wl.usage.cpu_cores)} / request {format_cpu(wl.requests.cpu_cores)} "
            f"({float(cpu_ratio * 100):.1f}% of request)"
        )

    print("\n### TOP MEMORY UTILIZATION (vs requests) ###\n")
    for wl in mem_sorted[:10]:
        mem_ratio = (
            wl.usage.memory_bytes / wl.requests.memory_bytes
            if wl.requests.memory_bytes > 0
            else Decimal("0")
        )
        print(
            f"{wl.namespace}/{wl.name} ({wl.kind}): "
            f"usage {format_memory(wl.usage.memory_bytes)} / request {format_memory(wl.requests.memory_bytes)} "
            f"({float(mem_ratio * 100):.1f}% of request)"
        )

    print("\n### SUGGESTED ADJUSTMENTS ###\n")
    if not suggestions:
        print("No suggestions. Requests and limits look balanced.")
    else:
        for sug in suggestions:
            print(
                f"[{sug.severity.upper()}] {sug.namespace}/{sug.workload} ({sug.kind}): {sug.message}"
            )


app = cyclopts.App(help="Analyze workloads with kubectl top to refine requests/limits")


@app.default
def main(
    *,
    kubeconfig: str | None = None,
    context: str | None = None,
    namespace: str | None = None,
    json_output: bool = False,
    detailed: bool = False,
) -> None:
    """Analyze live workload utilization versus requests/limits.

    Args:
        kubeconfig: Path to kubeconfig file (default: ~/.kube/config).
        context: Kubernetes context to use.
        namespace: Namespace to scope analysis (default: all namespaces).
        json_output: Output results as JSON.
        detailed: Include per-workload details.
    """
    try:
        if kubeconfig:
            config.load_kube_config(config_file=kubeconfig, context=context)
        else:
            config.load_kube_config(context=context)
    except Exception as exc:
        print(f"Error loading Kubernetes config: {exc}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Collecting workload usage via kubectl top (namespace: {namespace or 'all'})...",
        file=sys.stderr,
    )
    workloads, missing_metrics = collect_workload_usage(namespace)
    suggestions = suggest_adjustments(workloads)

    if json_output:
        result = {
            "namespace": namespace or "all",
            "workload_count": len(workloads),
            "missing_metrics": missing_metrics,
            "workloads": [
                {
                    "name": wl.name,
                    "namespace": wl.namespace,
                    "kind": wl.kind,
                    "requests": wl.requests.to_human_readable(),
                    "limits": wl.limits.to_human_readable(),
                    "usage": wl.usage.to_human_readable(),
                    "utilization": wl.utilization(),
                    "pods": sorted(wl.pods),
                    "missing_metrics": wl.missing_metrics,
                }
                for wl in workloads
            ],
            "suggestions": [
                {
                    "severity": sug.severity,
                    "namespace": sug.namespace,
                    "workload": sug.workload,
                    "kind": sug.kind,
                    "message": sug.message,
                }
                for sug in suggestions
            ],
        }
        print(json.dumps(result, indent=2))
    else:
        print_report(workloads, suggestions, missing_metrics, namespace)

        if detailed and workloads:
            print("\n### WORKLOAD DETAILS ###\n")
            for wl in sorted(workloads, key=lambda w: (w.namespace, w.name)):
                util = wl.utilization()
                print(f"{wl.namespace}/{wl.name} ({wl.kind})")
                print(f"  Pods: {len(wl.pods)}")
                print(
                    f"  CPU: usage {format_cpu(wl.usage.cpu_cores)} "
                    f"| request {format_cpu(wl.requests.cpu_cores)} "
                    f"| limit {format_cpu(wl.limits.cpu_cores)} "
                    f"| util {util['cpu'] * 100:.1f}%"
                )
                print(
                    f"  Mem: usage {format_memory(wl.usage.memory_bytes)} "
                    f"| request {format_memory(wl.requests.memory_bytes)} "
                    f"| limit {format_memory(wl.limits.memory_bytes)} "
                    f"| util {util['memory'] * 100:.1f}%"
                )
                if wl.missing_metrics:
                    print(f"  Missing metrics entries: {wl.missing_metrics}")


if __name__ == "__main__":
    app()
