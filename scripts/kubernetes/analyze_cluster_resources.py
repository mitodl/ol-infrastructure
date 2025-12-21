#!/usr/bin/env python3
"""Analyze Kubernetes cluster resource requirements and recommend baseline nodes.

This script:
1. Enumerates all stable workloads (Deployments, StatefulSets, DaemonSets)
2. Collects CPU and RAM requests/limits (static declarations)
3. Collects actual resource usage over time via kubetop/metrics-server
4. Aggregates resource requirements and observed usage
5. Analyzes current nodegroups and capacity
6. Recommends optimal baseline node configuration using AWS instance types
7. Provides bin packing analysis for efficient resource utilization

Requirements:
- kubectl configured and authenticated to cluster
- AWS CLI configured with credentials
- metrics-server running in cluster (for kubetop queries)
- Python 3.13+
- Libraries: boto3, kubernetes
"""

import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any

import boto3
import cyclopts
import httpx
from kubernetes import client, config

# Constants for resource conversion
MEMORY_UNITS = {
    "Ki": Decimal("1024"),
    "Mi": Decimal("1024") ** 2,
    "Gi": Decimal("1024") ** 3,
    "K": Decimal("1000"),
    "M": Decimal("1000") ** 2,
    "G": Decimal("1000") ** 3,
}

CPU_UNITS = {
    "m": Decimal("0.001"),  # millicores
    "n": Decimal("0.000000001"),  # nanocores
    "": Decimal("1"),  # default is cores
}

# Headroom buffer for autoscaling (20% by default)
DEFAULT_HEADROOM_PERCENTAGE = 20


class WorkloadType(Enum):
    """Kubernetes workload types."""

    DEPLOYMENT = "Deployment"
    STATEFULSET = "StatefulSet"
    DAEMONSET = "DaemonSet"
    POD = "Pod"


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
class WorkloadMetrics:
    """Metrics for a single workload."""

    name: str
    namespace: str
    kind: str
    replicas: int = 1
    requests: ResourceRequest = field(default_factory=ResourceRequest)
    limits: ResourceRequest = field(default_factory=ResourceRequest)
    actual_usage_avg: ResourceRequest = field(default_factory=ResourceRequest)
    actual_usage_peak: ResourceRequest = field(default_factory=ResourceRequest)
    actual_usage_p95: ResourceRequest = field(default_factory=ResourceRequest)

    def total_requests(self) -> ResourceRequest:
        """Get total requests across all replicas."""
        return ResourceRequest(
            cpu_cores=self.requests.cpu_cores * self.replicas,
            memory_bytes=self.requests.memory_bytes * self.replicas,
        )

    def total_limits(self) -> ResourceRequest:
        """Get total limits across all replicas."""
        return ResourceRequest(
            cpu_cores=self.limits.cpu_cores * self.replicas,
            memory_bytes=self.limits.memory_bytes * self.replicas,
        )

    def total_actual_usage_avg(self) -> ResourceRequest:
        """Get average actual usage across all replicas."""
        return ResourceRequest(
            cpu_cores=self.actual_usage_avg.cpu_cores * self.replicas,
            memory_bytes=self.actual_usage_avg.memory_bytes * self.replicas,
        )

    def total_actual_usage_peak(self) -> ResourceRequest:
        """Get peak actual usage across all replicas."""
        return ResourceRequest(
            cpu_cores=self.actual_usage_peak.cpu_cores * self.replicas,
            memory_bytes=self.actual_usage_peak.memory_bytes * self.replicas,
        )

    def total_actual_usage_p95(self) -> ResourceRequest:
        """Get 95th percentile actual usage across all replicas."""
        return ResourceRequest(
            cpu_cores=self.actual_usage_p95.cpu_cores * self.replicas,
            memory_bytes=self.actual_usage_p95.memory_bytes * self.replicas,
        )


@dataclass
class PodResourceProfile:
    """Profile of pod resource requirements (atomic unit)."""

    namespace: str
    workload_name: str
    workload_kind: str
    pod_count: int  # Number of pods with this profile
    cpu_cores: Decimal
    memory_bytes: Decimal

    def total_cpu(self) -> Decimal:
        """Total CPU for all pods with this profile."""
        return self.cpu_cores * self.pod_count

    def total_memory(self) -> Decimal:
        """Total memory for all pods with this profile."""
        return self.memory_bytes * self.pod_count

    def __hash__(self) -> int:
        """Make hashable for deduplication."""
        return hash((self.cpu_cores, self.memory_bytes))

    def __eq__(self, other: object) -> bool:
        """Compare by resource requirements."""
        if not isinstance(other, PodResourceProfile):
            return NotImplemented
        return (
            self.cpu_cores == other.cpu_cores
            and self.memory_bytes == other.memory_bytes
        )


@dataclass
class BinPackingResult:
    """Result of bin-packing analysis for an instance type."""

    instance_type: str
    cpu_cores: Decimal
    memory_gb: Decimal
    node_count: int
    pod_fit: dict[tuple[Decimal, Decimal], int]  # (cpu, mem) -> pods per node
    packing_efficiency: Decimal  # 0-100, higher is better
    cpu_utilization: Decimal  # Average CPU utilization per node
    memory_utilization: Decimal  # Average memory utilization per node
    fragmentation_waste: Decimal  # Wasted space due to poor packing

    def total_hourly_cost(self, price_per_hour: Decimal) -> Decimal:
        """Calculate hourly cost."""
        return price_per_hour * Decimal(self.node_count)


@dataclass
class NodeGroupMetrics:
    """Metrics for a nodegroup."""

    name: str
    instance_type: str
    node_count: int = 0
    desired_capacity: int = 0
    min_capacity: int = 0
    max_capacity: int = 0
    total_cpu_cores: Decimal = Decimal("0")
    total_memory_bytes: Decimal = Decimal("0")


@dataclass
class InstanceTypeCapacity:
    """Capacity of an AWS instance type."""

    instance_type: str
    cpu_cores: Decimal
    memory_gb: Decimal
    price_per_hour: Decimal = Decimal("0")
    generation: int = 0  # Generation number (e.g., 3, 4, 5, 6, 7)

    @property
    def memory_bytes(self) -> Decimal:
        """Get memory in bytes."""
        return self.memory_gb * Decimal("1024") ** 3

    @property
    def efficiency_score(self) -> Decimal:
        """Calculate efficiency score (higher is better for baseline).

        Prefers larger instances (higher score = larger instance).
        This encourages fewer, larger nodes over many small nodes.
        """
        # Score based on total capacity, with slight preference for larger instances
        # This will naturally select larger instances when efficiency is similar
        return self.cpu_cores * self.memory_gb


def extract_generation_from_instance_type(instance_type: str) -> int:
    """Extract generation number from instance type name.

    Examples:
        t3.micro -> 3
        m6i.xlarge -> 6
        c7g.2xlarge -> 7
        r5.large -> 5

    Returns 0 if generation cannot be extracted.
    """
    # Instance type format: family[subtype]generation[size]
    # Extract the digit(s) before the last period or end of string
    match = re.search(r"([a-z]+)(\d+)([a-z]*(?:\.|$))", instance_type)
    if match:
        try:
            return int(match.group(2))
        except (ValueError, IndexError):
            pass
    return 0


def parse_resource_quantity(quantity: str | None) -> Decimal:
    """Parse Kubernetes resource quantity to numeric value."""
    if not quantity:
        return Decimal("0")

    quantity = quantity.strip()
    for unit, multiplier in sorted(MEMORY_UNITS.items(), key=lambda x: -len(x[0])):
        if quantity.endswith(unit):
            return Decimal(quantity[: -len(unit)]) * multiplier

    for unit, multiplier in sorted(CPU_UNITS.items(), key=lambda x: -len(x[0])):
        if quantity.endswith(unit):
            return Decimal(quantity[: -len(unit)]) * multiplier

    return Decimal(quantity)


def parse_cpu_quantity(cpu_str: str | None) -> Decimal:
    """Parse CPU quantity to cores."""
    if not cpu_str:
        return Decimal("0")

    cpu_str = cpu_str.strip()

    # Check for known CPU units in order of specificity (longest first)
    for unit, multiplier in sorted(CPU_UNITS.items(), key=lambda x: -len(x[0])):
        if unit and cpu_str.endswith(unit):
            try:
                return Decimal(cpu_str[: -len(unit)]) * multiplier
            except Exception as e:
                print(
                    f"Warning: Failed to parse CPU quantity {cpu_str}: {e}",
                    file=sys.stderr,
                )
                return Decimal("0")

    # Default case: no unit suffix, interpret as cores
    try:
        return Decimal(cpu_str)
    except Exception as e:
        print(
            f"Warning: Failed to parse CPU quantity {cpu_str}: {e}",
            file=sys.stderr,
        )
        return Decimal("0")


def parse_memory_quantity(mem_str: str | None) -> Decimal:
    """Parse memory quantity to bytes."""
    if not mem_str:
        return Decimal("0")

    mem_str = mem_str.strip()

    for unit, multiplier in sorted(MEMORY_UNITS.items(), key=lambda x: -len(x[0])):
        if mem_str.endswith(unit):
            try:
                return Decimal(mem_str[: -len(unit)]) * multiplier
            except ValueError as e:
                print(
                    f"Warning: Failed to parse memory quantity {mem_str}: {e}",
                    file=sys.stderr,
                )
                continue

    return Decimal("0")


def get_pod_metrics(
    api_instance: client.CustomObjectsApi, namespace: str
) -> dict[str, dict[str, ResourceRequest]]:
    """Get actual resource usage metrics for pods in a namespace via metrics-server.

    Returns dict mapping pod name to {avg, peak, p95} ResourceRequest.
    """
    pod_metrics: dict[str, dict[str, ResourceRequest]] = {}

    try:
        metrics = api_instance.list_namespaced_custom_object(
            group="metrics.k8s.io",
            version="v1beta1",
            namespace=namespace,
            plural="pods",
        )

        for item in metrics.get("items", []):
            pod_name = item["metadata"]["name"]
            containers = item.get("containers", [])

            # Aggregate metrics across all containers in the pod
            total_cpu = Decimal("0")
            total_memory = Decimal("0")

            for container in containers:
                if "usage" in container:
                    usage = container["usage"]
                    cpu_str = usage.get("cpu", "0")
                    memory_str = usage.get("memory", "0")

                    total_cpu += parse_cpu_quantity(cpu_str)
                    total_memory += parse_memory_quantity(memory_str)

            if total_cpu > 0 or total_memory > 0:
                pod_metrics[pod_name] = {
                    "current": ResourceRequest(
                        cpu_cores=total_cpu,
                        memory_bytes=total_memory,
                    )
                }

    except client.exceptions.ApiException as e:
        if (
            e.status != 404
        ):  # 404 means metrics not available (OK if metrics-server not installed)
            print(
                f"Warning: Could not fetch pod metrics for {namespace}: {e}",
                file=sys.stderr,
            )

    return pod_metrics


def aggregate_pod_metrics(
    workloads: list[WorkloadMetrics],
    pod_metrics_by_namespace: dict[str, dict[str, dict[str, ResourceRequest]]],
) -> None:
    """Aggregate pod metrics into workload metrics.

    For each workload, collects metrics from its pods and calculates avg, peak, p95.
    Updates WorkloadMetrics in-place.
    """
    for workload in workloads:
        namespace = workload.namespace
        pod_metrics = pod_metrics_by_namespace.get(namespace, {})

        # Try to find pods matching this workload
        # This is a heuristic: pods created by Deployments/StatefulSets have names like "name-<random>"
        matching_pods = []
        for pod_name, metrics in pod_metrics.items():
            # Check if pod name starts with workload name
            if pod_name.startswith(workload.name + "-"):
                matching_pods.append(metrics.get("current", ResourceRequest()))

        if matching_pods:
            # Calculate statistics
            avg_cpu = sum(m.cpu_cores for m in matching_pods) / Decimal(
                len(matching_pods)
            )
            avg_memory = sum(m.memory_bytes for m in matching_pods) / Decimal(
                len(matching_pods)
            )
            peak_cpu = max(m.cpu_cores for m in matching_pods)
            peak_memory = max(m.memory_bytes for m in matching_pods)

            # Simple p95: sort and take 95th percentile
            sorted_cpu = sorted([m.cpu_cores for m in matching_pods])
            sorted_memory = sorted([m.memory_bytes for m in matching_pods])
            p95_idx = max(0, int(len(matching_pods) * 0.95) - 1)
            p95_cpu = sorted_cpu[p95_idx] if sorted_cpu else Decimal("0")
            p95_memory = sorted_memory[p95_idx] if sorted_memory else Decimal("0")

            workload.actual_usage_avg = ResourceRequest(
                cpu_cores=avg_cpu,
                memory_bytes=avg_memory,
            )
            workload.actual_usage_peak = ResourceRequest(
                cpu_cores=peak_cpu,
                memory_bytes=peak_memory,
            )
            workload.actual_usage_p95 = ResourceRequest(
                cpu_cores=p95_cpu,
                memory_bytes=p95_memory,
            )


def extract_pod_profiles(
    workloads: list[WorkloadMetrics],
) -> tuple[list[PodResourceProfile], ResourceRequest]:
    """Extract atomic pod resource profiles from workloads.

    Groups pods by their resource requirements (atomic units).
    Deduplicates profiles where the same pod type appears in multiple workloads.

    Returns:
        (profiles, daemonset_reserved_per_node) - Pod profiles list and per-node DaemonSet resource reservation
    """
    profiles: dict[tuple[Decimal, Decimal], PodResourceProfile] = {}
    daemonset_reserved = ResourceRequest()

    for wl in workloads:
        # Skip workloads with zero requests
        if wl.requests.cpu_cores == 0 and wl.requests.memory_bytes == 0:
            continue

        key = (wl.requests.cpu_cores, wl.requests.memory_bytes)

        # DaemonSets reserve resources on EVERY node, not as a pool
        if wl.kind == WorkloadType.DAEMONSET.value:
            daemonset_reserved += wl.requests
            # Don't add DaemonSets to regular pod profiles for bin-packing
            # since they're handled separately as per-node reservations
            continue

        if key not in profiles:
            profiles[key] = PodResourceProfile(
                namespace=wl.namespace,
                workload_name=wl.name,
                workload_kind=wl.kind,
                pod_count=wl.replicas,
                cpu_cores=wl.requests.cpu_cores,
                memory_bytes=wl.requests.memory_bytes,
            )
        else:
            # Aggregate pod count for duplicate profiles
            profiles[key].pod_count += wl.replicas

    pod_list = sorted(
        profiles.values(),
        key=lambda p: (-p.total_cpu(), -p.total_memory()),  # Largest first
    )

    return pod_list, daemonset_reserved


def bin_pack_pods(
    pod_profiles: list[PodResourceProfile],
    instance_type: str,
    instance_cpu: Decimal,
    instance_memory_gb: Decimal,
    daemonset_reserved: ResourceRequest | None = None,
) -> BinPackingResult:
    """Perform first-fit decreasing bin packing analysis.

    Simulates packing pods onto nodes of a given instance type.
    Accounts for per-node DaemonSet resource reservations.
    Returns packing efficiency and fragmentation metrics.

    Args:
        pod_profiles: List of pod resource profiles to pack
        instance_type: Name of the instance type
        instance_cpu: CPU cores per instance
        instance_memory_gb: Memory GB per instance
        daemonset_reserved: Per-node resource reservation for DaemonSets
    """
    instance_memory_bytes = instance_memory_gb * Decimal("1024") ** 3

    # Account for per-node DaemonSet reservations
    if daemonset_reserved is None:
        daemonset_reserved = ResourceRequest()

    available_cpu_per_node = instance_cpu - daemonset_reserved.cpu_cores
    available_memory_per_node = instance_memory_bytes - daemonset_reserved.memory_bytes

    # Track remaining space on each node
    nodes: list[dict[str, Decimal | int]] = []

    # Remaining pods to place
    remaining_pods = {(p.cpu_cores, p.memory_bytes): p.pod_count for p in pod_profiles}

    # First-fit decreasing: try to fit largest pods first
    for profile in pod_profiles:
        pod_cpu = profile.cpu_cores
        pod_mem = profile.memory_bytes
        pods_to_place = remaining_pods.get((pod_cpu, pod_mem), 0)

        while pods_to_place > 0:
            # Find a node with enough space
            placed = False

            for node in nodes:
                if node["cpu"] >= pod_cpu and node["memory"] >= pod_mem:
                    node["cpu"] -= pod_cpu
                    node["memory"] -= pod_mem
                    node["pod_count"] += 1
                    pods_to_place -= 1
                    placed = True
                    break

            # If no node has space, create a new one
            if not placed:
                nodes.append(
                    {
                        "cpu": available_cpu_per_node - pod_cpu,
                        "memory": available_memory_per_node - pod_mem,
                        "pod_count": 1,
                    }
                )
                pods_to_place -= 1

    # Calculate metrics
    node_count = len(nodes)

    if node_count == 0:
        return BinPackingResult(
            instance_type=instance_type,
            cpu_cores=instance_cpu,
            memory_gb=instance_memory_gb,
            node_count=0,
            pod_fit={},
            packing_efficiency=Decimal("0"),
            cpu_utilization=Decimal("0"),
            memory_utilization=Decimal("0"),
            fragmentation_waste=Decimal("0"),
        )

    # Calculate utilization
    # Total used = (instance capacity - remaining space) per node
    total_cpu_used = available_cpu_per_node * Decimal(node_count) - sum(
        n["cpu"] for n in nodes
    )
    total_memory_used = available_memory_per_node * Decimal(node_count) - sum(
        n["memory"] for n in nodes
    )

    # Utilization is calculated against available capacity (excluding DaemonSet reservations)
    avg_cpu_util = (
        (total_cpu_used / (available_cpu_per_node * Decimal(node_count))) * Decimal(100)
        if available_cpu_per_node > 0
        else Decimal(0)
    )
    avg_mem_util = (
        (
            (total_memory_used / (available_memory_per_node * Decimal(node_count)))
            * Decimal(100)
        )
        if available_memory_per_node > 0
        else Decimal(0)
    )

    # Packing efficiency: min(CPU util, Memory util) to show bottleneck
    packing_eff = min(avg_cpu_util, avg_mem_util)

    # Fragmentation waste: average wasted space per node (as percentage of available capacity)
    total_waste = sum(
        n["cpu"] / available_cpu_per_node + n["memory"] / available_memory_per_node
        for n in nodes
        if available_cpu_per_node > 0 and available_memory_per_node > 0
    )
    avg_waste_percent = (
        (total_waste / Decimal(node_count) * Decimal(100))
        if available_cpu_per_node > 0 and available_memory_per_node > 0
        else Decimal(0)
    )

    # Pod fit distribution
    pod_fit: dict[tuple[Decimal, Decimal], int] = {}
    for profile in pod_profiles:
        # Count how many of this profile fit per node
        pods_per_node = 0
        for node in nodes:
            if (
                available_cpu_per_node - node["cpu"] >= profile.cpu_cores
                and available_memory_per_node - node["memory"] >= profile.memory_bytes
            ):
                pods_per_node += 1
        if pods_per_node > 0:
            pod_fit[(profile.cpu_cores, profile.memory_bytes)] = pods_per_node

    return BinPackingResult(
        instance_type=instance_type,
        cpu_cores=instance_cpu,
        memory_gb=instance_memory_gb,
        node_count=node_count,
        pod_fit=pod_fit,
        packing_efficiency=packing_eff,
        cpu_utilization=avg_cpu_util,
        memory_utilization=avg_mem_util,
        fragmentation_waste=avg_waste_percent,
    )


def get_workload_metrics(api_instance: client.AppsV1Api) -> list[WorkloadMetrics]:
    """Get resource metrics for all stable workloads."""
    workloads: list[WorkloadMetrics] = []

    try:
        # Get Deployments
        deployments = api_instance.list_deployment_for_all_namespaces()
        for dep in deployments.items:
            if dep.spec.replicas == 0:
                continue

            metrics = WorkloadMetrics(
                name=dep.metadata.name,
                namespace=dep.metadata.namespace,
                kind=WorkloadType.DEPLOYMENT.value,
                replicas=dep.spec.replicas or 1,
            )

            for container in dep.spec.template.spec.containers:
                if container.resources.requests:
                    metrics.requests += ResourceRequest(
                        cpu_cores=parse_cpu_quantity(
                            container.resources.requests.get("cpu")
                        ),
                        memory_bytes=parse_memory_quantity(
                            container.resources.requests.get("memory")
                        ),
                    )
                if container.resources.limits:
                    metrics.limits += ResourceRequest(
                        cpu_cores=parse_cpu_quantity(
                            container.resources.limits.get("cpu")
                        ),
                        memory_bytes=parse_memory_quantity(
                            container.resources.limits.get("memory")
                        ),
                    )

            workloads.append(metrics)

        # Get StatefulSets
        statefulsets = api_instance.list_stateful_set_for_all_namespaces()
        for ss in statefulsets.items:
            if ss.spec.replicas == 0:
                continue

            metrics = WorkloadMetrics(
                name=ss.metadata.name,
                namespace=ss.metadata.namespace,
                kind=WorkloadType.STATEFULSET.value,
                replicas=ss.spec.replicas or 1,
            )

            for container in ss.spec.template.spec.containers:
                if container.resources.requests:
                    metrics.requests += ResourceRequest(
                        cpu_cores=parse_cpu_quantity(
                            container.resources.requests.get("cpu")
                        ),
                        memory_bytes=parse_memory_quantity(
                            container.resources.requests.get("memory")
                        ),
                    )
                if container.resources.limits:
                    metrics.limits += ResourceRequest(
                        cpu_cores=parse_cpu_quantity(
                            container.resources.limits.get("cpu")
                        ),
                        memory_bytes=parse_memory_quantity(
                            container.resources.limits.get("memory")
                        ),
                    )

            workloads.append(metrics)

        # Get DaemonSets
        daemonsets = api_instance.list_daemon_set_for_all_namespaces()
        for ds in daemonsets.items:
            metrics = WorkloadMetrics(
                name=ds.metadata.name,
                namespace=ds.metadata.namespace,
                kind=WorkloadType.DAEMONSET.value,
                replicas=1,  # DaemonSets are counted per node
            )

            for container in ds.spec.template.spec.containers:
                if container.resources.requests:
                    metrics.requests += ResourceRequest(
                        cpu_cores=parse_cpu_quantity(
                            container.resources.requests.get("cpu")
                        ),
                        memory_bytes=parse_memory_quantity(
                            container.resources.requests.get("memory")
                        ),
                    )
                if container.resources.limits:
                    metrics.limits += ResourceRequest(
                        cpu_cores=parse_cpu_quantity(
                            container.resources.limits.get("cpu")
                        ),
                        memory_bytes=parse_memory_quantity(
                            container.resources.limits.get("memory")
                        ),
                    )

            workloads.append(metrics)

    except client.exceptions.ApiException as e:
        print(f"Error fetching workloads: {e}", file=sys.stderr)

    return workloads


def get_node_metrics(api_instance: client.CoreV1Api) -> dict[str, dict[str, Any]]:
    """Get metrics for all nodes."""
    nodes_info: dict[str, dict[str, Any]] = {}

    try:
        nodes = api_instance.list_node()
        for node in nodes.items:
            node_name = node.metadata.name
            nodes_info[node_name] = {
                "name": node_name,
                "allocatable_cpu": parse_cpu_quantity(
                    node.status.allocatable.get("cpu")
                ),
                "allocatable_memory": parse_memory_quantity(
                    node.status.allocatable.get("memory")
                ),
                "capacity_cpu": parse_cpu_quantity(node.status.capacity.get("cpu")),
                "capacity_memory": parse_memory_quantity(
                    node.status.capacity.get("memory")
                ),
                "labels": node.metadata.labels or {},
            }
    except client.exceptions.ApiException as e:
        print(f"Error fetching nodes: {e}", file=sys.stderr)

    return nodes_info


def get_nodegroup_info(region: str, cluster_name: str) -> list[NodeGroupMetrics]:
    """Get nodegroup information from AWS EKS."""
    nodegroups: list[NodeGroupMetrics] = []

    try:
        eks_client = boto3.client("eks", region_name=region)
        ec2_client = boto3.client("ec2", region_name=region)

        # List nodegroups
        response = eks_client.list_nodegroups(clusterName=cluster_name)

        for ng_name in response.get("nodegroups", []):
            try:
                ng_detail = eks_client.describe_nodegroup(
                    clusterName=cluster_name, nodegroupName=ng_name
                )
                ng = ng_detail["nodegroup"]

                # Get instance type
                instance_types = ng.get("instanceTypes", [])
                instance_type = instance_types[0] if instance_types else "unknown"

                metrics = NodeGroupMetrics(
                    name=ng_name,
                    instance_type=instance_type,
                    node_count=ng.get("resources", {}).get("nodes", 0),
                    desired_capacity=ng.get("scalingConfig", {}).get("desiredSize", 0),
                    min_capacity=ng.get("scalingConfig", {}).get("minSize", 0),
                    max_capacity=ng.get("scalingConfig", {}).get("maxSize", 0),
                )

                # Get instance type details
                if instance_type != "unknown":
                    try:
                        instances = ec2_client.describe_instance_types(
                            InstanceTypes=[instance_type]
                        )
                        if instances["InstanceTypes"]:
                            inst = instances["InstanceTypes"][0]
                            vcpu_count = inst.get("VCpuInfo", {}).get("DefaultVCpus", 0)
                            mem_gib = inst.get("MemoryInfo", {}).get("SizeInMiB", 0)

                            metrics.total_cpu_cores = Decimal(vcpu_count)
                            metrics.total_memory_bytes = (
                                Decimal(mem_gib) * Decimal("1024") ** 2
                            )

                            # Calculate totals for nodegroup
                            if metrics.node_count > 0:
                                metrics.total_cpu_cores = (
                                    metrics.total_cpu_cores * metrics.node_count
                                )
                                metrics.total_memory_bytes = (
                                    metrics.total_memory_bytes * metrics.node_count
                                )
                    except Exception as e:
                        print(
                            f"Error fetching instance type {instance_type}: {e}",
                            file=sys.stderr,
                        )

                nodegroups.append(metrics)

            except Exception as e:
                print(f"Error fetching nodegroup {ng_name}: {e}", file=sys.stderr)

    except Exception as e:
        print(f"Error connecting to AWS EKS: {e}", file=sys.stderr)

    return nodegroups


def get_instance_types(region: str) -> dict[str, InstanceTypeCapacity]:
    """Get available instance types from AWS."""
    instance_types: dict[str, InstanceTypeCapacity] = {}

    try:
        ec2_client = boto3.client("ec2", region_name=region)

        paginator = ec2_client.get_paginator("describe_instance_types")
        pages = paginator.paginate()

        for page in pages:
            for inst in page.get("InstanceTypes", []):
                inst_type = inst["InstanceType"]
                vcpu = inst.get("VCpuInfo", {}).get("DefaultVCpus", 0)
                memory_mib = inst.get("MemoryInfo", {}).get("SizeInMiB", 0)

                # Filter for x86_64 architecture
                processor_info = inst.get("ProcessorInfo", {})
                supported_arch = processor_info.get("SupportedArchitectures", [])
                if (
                    vcpu > 0
                    and memory_mib > 0
                    and ("x86_64" in supported_arch or "x86" in supported_arch)
                ):
                    instance_types[inst_type] = InstanceTypeCapacity(
                        instance_type=inst_type,
                        cpu_cores=Decimal(vcpu),
                        memory_gb=Decimal(memory_mib) / Decimal("1024"),
                        generation=extract_generation_from_instance_type(inst_type),
                    )

    except Exception as e:
        print(f"Error fetching instance types: {e}", file=sys.stderr)

    return instance_types


def get_instance_pricing(
    region: str, instance_types: dict[str, InstanceTypeCapacity]
) -> None:
    """Fetch on-demand pricing for instance types and update in-place."""
    try:
        pricing_client = boto3.client("pricing", region_name="us-east-1")

        # Pricing API is only available in us-east-1
        # We'll fetch all prices and filter by region
        paginator = pricing_client.get_paginator("get_products")
        page_iterator = paginator.paginate(
            ServiceCode="AmazonEC2",
            Filters=[
                {
                    "Type": "TERM_MATCH",
                    "Field": "location",
                    "Value": _get_pricing_region(region),
                },
                {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                {
                    "Type": "TERM_MATCH",
                    "Field": "preInstalledSw",
                    "Value": "NA",
                },
                {
                    "Type": "TERM_MATCH",
                    "Field": "licenseModel",
                    "Value": "No License required",
                },
                {
                    "Type": "TERM_MATCH",
                    "Field": "capacitystatus",
                    "Value": "Used",
                },
            ],
        )

        prices_by_type: dict[str, Decimal] = {}

        for page in page_iterator:
            for price_item in page.get("PriceList", []):
                try:
                    price_data = json.loads(price_item)
                    product = price_data.get("product", {})
                    attributes = product.get("attributes", {})
                    inst_type = attributes.get("instanceType")

                    if inst_type and inst_type in instance_types:
                        on_demand = price_data.get("terms", {}).get("OnDemand", {})
                        if on_demand:
                            # Get the first (and usually only) on-demand pricing
                            price_key = next(iter(on_demand.keys()))
                            price_dimensions = on_demand[price_key].get(
                                "priceDimensions", {}
                            )
                            if price_dimensions:
                                dimension_key = next(iter(price_dimensions.keys()))
                                price_str = (
                                    price_dimensions[dimension_key]
                                    .get("pricePerUnit", {})
                                    .get("USD", "0")
                                )
                                prices_by_type[inst_type] = Decimal(price_str)
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue

        # Update instance types with pricing
        for inst_type, price in prices_by_type.items():
            if inst_type in instance_types:
                instance_types[inst_type].price_per_hour = price

        # Log pricing fetch results
        priced_count = sum(1 for it in instance_types.values() if it.price_per_hour > 0)
        if priced_count > 0:
            print(
                f"Successfully fetched pricing for {priced_count}/{len(instance_types)} instance types",
                file=sys.stderr,
            )
            # Log which instance types are missing pricing
            unpriced_types = [
                name for name, it in instance_types.items() if it.price_per_hour == 0
            ]
            if unpriced_types:
                print(
                    f"Warning: {len(unpriced_types)} instance types have no pricing data: {', '.join(sorted(unpriced_types)[:10])}{'...' if len(unpriced_types) > 10 else ''}",
                    file=sys.stderr,
                )

    except Exception as e:
        print(f"Warning: Could not fetch pricing data: {e}", file=sys.stderr)


def _get_pricing_region(aws_region: str) -> str:
    """Convert AWS region code to pricing API location string."""
    region_mapping = {
        "us-east-1": "US East (N. Virginia)",
        "us-east-2": "US East (Ohio)",
        "us-west-1": "US West (N. California)",
        "us-west-2": "US West (Oregon)",
        "eu-west-1": "EU (Ireland)",
        "eu-west-2": "EU (London)",
        "eu-central-1": "EU (Frankfurt)",
        "ap-northeast-1": "Asia Pacific (Tokyo)",
        "ap-southeast-1": "Asia Pacific (Singapore)",
        "ap-southeast-2": "Asia Pacific (Sydney)",
    }
    return region_mapping.get(aws_region, aws_region)


def debug_instance_type_pricing(
    instance_type: str, instance_types: dict[str, InstanceTypeCapacity]
) -> None:
    """Debug pricing for a specific instance type."""
    if instance_type not in instance_types:
        print(
            f"Instance type '{instance_type}' not found in available types",
            file=sys.stderr,
        )
        return

    capacity = instance_types[instance_type]
    print(f"\nDEBUG: {instance_type}", file=sys.stderr)
    print("  Found in instance types: Yes", file=sys.stderr)
    print(f"  CPU: {capacity.cpu_cores}", file=sys.stderr)
    print(f"  Memory: {capacity.memory_gb} GB", file=sys.stderr)
    print(f"  Price per hour: ${float(capacity.price_per_hour):.4f}", file=sys.stderr)
    if capacity.price_per_hour == 0:
        print("  ⚠️  WARNING: No pricing data available!", file=sys.stderr)


def get_instance_pricing_fallback(
    region: str, instance_types: dict[str, InstanceTypeCapacity]
) -> None:
    """Fetch pricing from instances.vantage.sh as fallback."""
    try:
        # Map AWS regions to vantage.sh region IDs
        region_mapping = {
            "us-east-1": "us-east-1",
            "us-east-2": "us-east-2",
            "us-west-1": "us-west-1",
            "us-west-2": "us-west-2",
            "eu-west-1": "eu-west-1",
            "eu-central-1": "eu-central-1",
            "ap-northeast-1": "ap-northeast-1",
            "ap-southeast-1": "ap-southeast-1",
            "ap-southeast-2": "ap-southeast-2",
        }

        vantage_region = region_mapping.get(region, region)

        # Fetch pricing data from instances.vantage.sh
        url = f"https://instances.vantage.sh/api/ec2/pricing/{vantage_region}/on-demand/index.json"
        with httpx.Client(timeout=10) as client:
            response = client.get(url)
            response.raise_for_status()
            pricing_data = response.json()

        # Parse and apply pricing
        for inst_type, price_info in pricing_data.items():
            if inst_type in instance_types:
                try:
                    hourly_price = float(price_info.get("price", 0))
                    instance_types[inst_type].price_per_hour = Decimal(
                        str(hourly_price)
                    )
                except (ValueError, TypeError):
                    continue

        priced_count = sum(1 for it in instance_types.values() if it.price_per_hour > 0)
        print(
            f"Fetched fallback pricing from vantage.sh for {priced_count}/{len(instance_types)} instance types",
            file=sys.stderr,
        )
        # Log which instance types are still missing pricing
        unpriced_types = [
            name for name, it in instance_types.items() if it.price_per_hour == 0
        ]
        if unpriced_types:
            print(
                f"Warning: {len(unpriced_types)} instance types still have no pricing data: {', '.join(sorted(unpriced_types)[:10])}{'...' if len(unpriced_types) > 10 else ''}",
                file=sys.stderr,
            )
    except Exception as e:
        print(
            f"Warning: Could not fetch fallback pricing from vantage.sh: {e}",
            file=sys.stderr,
        )


def recommend_with_bin_packing(
    pod_profiles: list[PodResourceProfile],
    instance_types: dict[str, InstanceTypeCapacity],
    headroom_percentage: int = DEFAULT_HEADROOM_PERCENTAGE,  # noqa: ARG001
    daemonset_reserved: ResourceRequest | None = None,
) -> list[tuple[BinPackingResult, Decimal]]:
    """Recommend instance types optimized for bin-packing efficiency.

    Performs bin-packing analysis on each instance type and ranks by packing efficiency.
    Accounts for per-node DaemonSet resource reservations.
    Returns sorted list of (BinPackingResult, monthly_cost) tuples.

    Args:
        pod_profiles: List of pod resource profiles to pack
        instance_types: Available AWS instance types
        headroom_percentage: Headroom percentage for autoscaling
        daemonset_reserved: Per-node resource reservation for DaemonSets
    """
    results: list[tuple[BinPackingResult, Decimal]] = []

    # Filter for general purpose instances
    suitable_types = {
        k: v
        for k, v in instance_types.items()
        if any(
            prefix in k for prefix in ["t3", "t4", "m5", "m6", "m7", "c5", "c6", "c7"]
        )
    }

    if not suitable_types:
        suitable_types = instance_types

    for inst_type, capacity in suitable_types.items():
        # Perform bin-packing simulation with DaemonSet reservation
        bin_result = bin_pack_pods(
            pod_profiles,
            inst_type,
            capacity.cpu_cores,
            capacity.memory_gb,
            daemonset_reserved=daemonset_reserved,
        )

        # Enforce minimum 3 nodes for HA
        final_node_count = max(bin_result.node_count, 3)

        # Scale up nodes if we've enforced the minimum
        if final_node_count > bin_result.node_count:
            bin_result.node_count = final_node_count

        # Calculate cost
        hourly_cost = capacity.price_per_hour * Decimal(final_node_count)
        monthly_cost = hourly_cost * Decimal("730")

        results.append((bin_result, monthly_cost))

    # Sort by packing efficiency (higher is better), then by cost
    results.sort(key=lambda x: (-x[0].packing_efficiency, x[1]))

    return results


def recommend_baseline_nodes(
    required_cpu: Decimal,
    required_memory_gb: Decimal,
    instance_types: dict[str, InstanceTypeCapacity],
    headroom_percentage: int = DEFAULT_HEADROOM_PERCENTAGE,
) -> dict[str, Any]:
    """Recommend optimal baseline node configuration.

    Biases toward fewer, larger nodes for better operational efficiency.
    Enforces a minimum of 3 nodes for high availability and fault tolerance.
    """
    # Add headroom
    required_cpu_with_headroom = (
        required_cpu * Decimal(100 + headroom_percentage) / Decimal(100)
    )
    required_memory_with_headroom = (
        required_memory_gb * Decimal(100 + headroom_percentage) / Decimal(100)
    )

    best_config: tuple[str, int, Decimal, Decimal] | None = (
        None  # (type, count, waste, score)
    )
    all_configs: list[dict[str, Any]] = []

    # Filter for general purpose instances (t3, m5, m6, m7, c5, c6, c7)
    suitable_types = {
        k: v
        for k, v in instance_types.items()
        if any(
            prefix in k for prefix in ["t3", "t4", "m5", "m6", "m7", "c5", "c6", "c7"]
        )
    }

    if not suitable_types:
        suitable_types = instance_types

    for inst_type, capacity in sorted(
        suitable_types.items(), key=lambda x: -x[1].efficiency_score
    ):
        # Calculate how many nodes needed
        nodes_for_cpu = int(
            (required_cpu_with_headroom / capacity.cpu_cores).to_integral_value()
        )
        nodes_for_memory = int(
            (required_memory_with_headroom / capacity.memory_gb).to_integral_value()
        )

        # Enforce minimum of 3 nodes for high availability
        nodes_needed = max(nodes_for_cpu, nodes_for_memory, 3)

        total_cpu = capacity.cpu_cores * nodes_needed
        total_memory = capacity.memory_gb * nodes_needed

        # Calculate waste (overprovisioned resources)
        cpu_waste = (
            (total_cpu - required_cpu_with_headroom)
            / required_cpu_with_headroom
            * Decimal("100")
        )
        memory_waste = (
            (total_memory - required_memory_with_headroom)
            / required_memory_with_headroom
            * Decimal("100")
        )
        avg_waste = (cpu_waste + memory_waste) / Decimal("2")

        # Calculate hourly cost
        hourly_cost = capacity.price_per_hour * Decimal(nodes_needed)
        monthly_cost = hourly_cost * Decimal("730")  # ~730 hours/month
        yearly_cost = monthly_cost * Decimal("12")

        # Scoring: prefer low waste, low cost, newer generations, and fewer nodes
        # Penalties (lower is better):
        #  - waste_penalty: avg_waste percent
        #  - cost_penalty: hourly_cost (normalized by dividing by 10)
        #  - node_preference_penalty: nodes_needed (prefer fewer nodes)
        #  - generation_bonus: -capacity.generation (newer generations score better)
        node_preference_penalty = Decimal(nodes_needed) * Decimal("0.1")
        cost_penalty = hourly_cost / Decimal("10")  # Normalize cost to be comparable
        generation_bonus = -Decimal(max(capacity.generation, 1)) * Decimal(
            "0.5"
        )  # Newer is better
        config_score = (
            avg_waste + node_preference_penalty + cost_penalty + generation_bonus
        )

        config = {
            "instance_type": inst_type,
            "generation": capacity.generation,
            "instance_vcpu": float(capacity.cpu_cores),
            "instance_memory_gb": float(capacity.memory_gb),
            "node_count": int(nodes_needed),
            "total_cpu": float(total_cpu),
            "total_memory_gb": float(total_memory),
            "cpu_waste_percent": float(cpu_waste),
            "memory_waste_percent": float(memory_waste),
            "avg_waste_percent": float(avg_waste),
            "price_per_hour_per_node": float(capacity.price_per_hour),
            "total_hourly_cost": float(hourly_cost),
            "total_monthly_cost": float(monthly_cost),
            "total_yearly_cost": float(yearly_cost),
            "config_score": float(config_score),
        }

        all_configs.append(config)

        # Find best config (lowest score which includes waste + node preference)
        if best_config is None or config_score < best_config[3]:
            best_config = (inst_type, int(nodes_needed), avg_waste, config_score)

    # Sort by config score (waste + node preference)
    all_configs.sort(key=lambda x: x["config_score"])

    recommended_config = all_configs[0] if all_configs else None

    return {
        "recommended": {
            "instance_type": recommended_config["instance_type"]
            if recommended_config
            else None,
            "generation": recommended_config["generation"] if recommended_config else 0,
            "instance_vcpu": recommended_config["instance_vcpu"]
            if recommended_config
            else 0,
            "instance_memory_gb": recommended_config["instance_memory_gb"]
            if recommended_config
            else 0,
            "node_count": recommended_config["node_count"] if recommended_config else 0,
            "avg_waste_percent": recommended_config["avg_waste_percent"]
            if recommended_config
            else 0,
            "total_hourly_cost": recommended_config["total_hourly_cost"]
            if recommended_config
            else 0,
            "total_monthly_cost": recommended_config["total_monthly_cost"]
            if recommended_config
            else 0,
            "total_yearly_cost": recommended_config["total_yearly_cost"]
            if recommended_config
            else 0,
        },
        "top_alternatives": all_configs[:5],
    }


def print_bin_packing_analysis(
    pod_profiles: list[PodResourceProfile],
    bin_packing_results: list[tuple[BinPackingResult, Decimal]],
    daemonset_reserved: ResourceRequest | None = None,
) -> None:
    """Print bin-packing analysis report.

    Args:
        pod_profiles: Pod resource profiles
        bin_packing_results: Bin-packing results for each instance type
        daemonset_reserved: Per-node DaemonSet resource reservation
    """
    print("\n### BIN-PACKING ANALYSIS (Atomic Pod Units) ###\n")
    print(f"Unique pod resource profiles: {len(pod_profiles)}")
    print(f"Total pods: {sum(p.pod_count for p in pod_profiles)}")

    # Show DaemonSet reservations if present
    if daemonset_reserved and (
        daemonset_reserved.cpu_cores > 0 or daemonset_reserved.memory_bytes > 0
    ):
        ds_cpu = float(daemonset_reserved.cpu_cores)
        ds_mem = float(daemonset_reserved.memory_bytes / (1024**3))
        print(f"\nPer-node DaemonSet Reservation: {ds_cpu:.3f} CPU, {ds_mem:.2f} GB")
        print(
            "  (This capacity is reserved on every node and not available for regular workloads)"
        )

    print("\nPod Profiles (sorted by resource requirement):")
    for profile in pod_profiles:
        print(
            f"  {profile.workload_name} ({profile.workload_kind}): "
            f"{profile.pod_count} pods x {float(profile.cpu_cores):.3f}C {float(profile.memory_bytes / (1024**3)):.2f}GB"
        )

    print("\n### BIN-PACKING EFFICIENCY RANKINGS ###\n")
    for i, (bin_result, monthly_cost) in enumerate(bin_packing_results[:10], 1):
        print(f"{i}. {bin_result.instance_type}")
        print(
            f"   Instance: {float(bin_result.cpu_cores):.0f}C {float(bin_result.memory_gb):.0f}GB"
        )
        print(f"   Nodes needed: {bin_result.node_count}")
        print(f"   Packing efficiency: {float(bin_result.packing_efficiency):.1f}%")
        print(f"   CPU utilization: {float(bin_result.cpu_utilization):.1f}%")
        print(f"   Memory utilization: {float(bin_result.memory_utilization):.1f}%")
        print(f"   Fragmentation waste: {float(bin_result.fragmentation_waste):.1f}%")
        print(f"   Monthly cost: ${float(monthly_cost):.2f}")

        # Show pod fit distribution
        if bin_result.pod_fit:
            print("   Pod fit per node:")
            for (cpu, mem), count in sorted(bin_result.pod_fit.items()):
                print(
                    f"     {float(cpu):.3f}C {float(mem / (1024**3)):.2f}GB → {count} pods/node"
                )
        print()


def print_report(
    workloads: list[WorkloadMetrics],
    nodegroups: list[NodeGroupMetrics],
    nodes_info: dict[str, dict[str, Any]],
    recommendation: dict[str, Any],
    pod_profiles: list[PodResourceProfile] | None = None,
    bin_packing_results: list[tuple[BinPackingResult, Decimal]] | None = None,
    headroom_percentage: int = DEFAULT_HEADROOM_PERCENTAGE,
    daemonset_reserved: ResourceRequest | None = None,
):
    """Print analysis report."""
    print("\n" + "=" * 80)
    print("KUBERNETES CLUSTER RESOURCE ANALYSIS")
    print("=" * 80)

    # Aggregate workload metrics
    total_requests = sum(
        (wl.total_requests() for wl in workloads),
        ResourceRequest(),
    )
    total_limits = sum((wl.total_limits() for wl in workloads), ResourceRequest())

    print("\n### WORKLOAD SUMMARY ###\n")
    print(f"Total workloads: {len(workloads)}")
    print("\nAggregated Requests (declared):")
    for key, value in total_requests.to_human_readable().items():
        print(f"  {key}: {value}")
    print("\nAggregated Limits (declared):")
    for key, value in total_limits.to_human_readable().items():
        print(f"  {key}: {value}")

    # Show actual usage metrics if available
    total_actual_avg = sum(
        (wl.total_actual_usage_avg() for wl in workloads),
        ResourceRequest(),
    )
    total_actual_peak = sum(
        (wl.total_actual_usage_peak() for wl in workloads),
        ResourceRequest(),
    )
    total_actual_p95 = sum(
        (wl.total_actual_usage_p95() for wl in workloads),
        ResourceRequest(),
    )

    if total_actual_avg.cpu_cores > 0 or total_actual_avg.memory_bytes > 0:
        print("\nActual Usage (observed via metrics-server):")
        print(
            f"  Average: {float(total_actual_avg.cpu_cores):.3f} CPU, {float(total_actual_avg.memory_bytes / (1024**3)):.2f} GB"
        )
        print(
            f"  Peak:    {float(total_actual_peak.cpu_cores):.3f} CPU, {float(total_actual_peak.memory_bytes / (1024**3)):.2f} GB"
        )
        print(
            f"  P95:     {float(total_actual_p95.cpu_cores):.3f} CPU, {float(total_actual_p95.memory_bytes / (1024**3)):.2f} GB"
        )

    print("\n### WORKLOAD BREAKDOWN ###\n")
    workload_by_type: dict[str, ResourceRequest] = defaultdict(
        lambda: ResourceRequest()
    )
    for wl in workloads:
        workload_by_type[wl.kind] += wl.total_requests()

    for kind in sorted(workload_by_type.keys()):
        res = workload_by_type[kind]
        print(f"{kind}:")
        print(f"  CPU: {float(res.cpu_cores):.3f} cores ({int(res.cpu_cores * 1000)}m)")
        print(f"  Memory: {float(res.memory_bytes / (1024**3)):.2f} GB")

    print("\n### CURRENT CLUSTER CAPACITY ###\n")

    if nodegroups:
        print("NodeGroups:")
        total_ng_cpu = Decimal("0")
        total_ng_memory = Decimal("0")
        for ng in nodegroups:
            print(f"\n  {ng.name}:")
            print(f"    Instance Type: {ng.instance_type}")
            print(f"    Current Nodes: {ng.node_count}")
            print(f"    Desired Capacity: {ng.desired_capacity}")
            print(f"    Min/Max: {ng.min_capacity}/{ng.max_capacity}")
            print(f"    Total CPU: {float(ng.total_cpu_cores):.1f} cores")
            print(
                f"    Total Memory: {float(ng.total_memory_bytes / (1024**3)):.2f} GB"
            )
            total_ng_cpu += ng.total_cpu_cores
            total_ng_memory += ng.total_memory_bytes

        print("\nTotal Capacity (across all nodegroups):")
        print(f"  CPU: {float(total_ng_cpu):.1f} cores")
        print(f"  Memory: {float(total_ng_memory / (1024**3)):.2f} GB")

        if total_ng_cpu > 0:
            cpu_utilization = total_requests.cpu_cores / total_ng_cpu * Decimal("100")
            print(f"\nEstimated CPU Utilization: {float(cpu_utilization):.1f}%%")
        if total_ng_memory > 0:
            mem_utilization = (
                total_requests.memory_bytes / total_ng_memory * Decimal("100")
            )
            print(f"Estimated Memory Utilization: {float(mem_utilization):.1f}%%")

    elif nodes_info:
        print(f"Nodes: {len(nodes_info)}")
        total_node_cpu = sum(Decimal(n["allocatable_cpu"]) for n in nodes_info.values())
        total_node_memory = sum(
            Decimal(n["allocatable_memory"]) for n in nodes_info.values()
        )
        print(f"  Total Allocatable CPU: {float(total_node_cpu):.1f} cores")
        print(
            f"  Total Allocatable Memory: {float(total_node_memory / (1024**3)):.2f} GB"
        )

    # Print bin-packing analysis if available
    if pod_profiles and bin_packing_results:
        print_bin_packing_analysis(
            pod_profiles, bin_packing_results, daemonset_reserved
        )

    print("\n### BASELINE NODE RECOMMENDATION ###\n")

    req_cpu = total_requests.cpu_cores
    req_memory_gb = total_requests.memory_bytes / Decimal("1024") ** 3

    rec = recommendation["recommended"]
    print(
        f"Recommended Instance Type: {rec['instance_type']} (Generation {rec['generation']})"
    )
    print(
        f"  Instance Spec: {rec['instance_vcpu']:.0f} vCPU, {rec['instance_memory_gb']:.0f} GB RAM"
    )
    print(f"Recommended Node Count: {rec['node_count']}")
    print(f"Estimated Resource Waste: {rec['avg_waste_percent']:.1f}%%")
    headroom_multiplier = Decimal(100 + headroom_percentage) / Decimal(100)
    print(f"\nRequired Resources (with {headroom_percentage}%% headroom):")
    print(f"  CPU: {float(req_cpu * headroom_multiplier):.1f} cores")
    print(f"  Memory: {float(req_memory_gb * headroom_multiplier):.2f} GB")

    print("\n### ESTIMATED COSTS ###\n")
    print("Recommended Configuration:")
    print(f"  Hourly Cost:  ${rec['total_hourly_cost']:>10.2f}")
    print(f"  Monthly Cost: ${rec['total_monthly_cost']:>10.2f} (~730 hours/month)")
    print(f"  Yearly Cost:  ${rec['total_yearly_cost']:>10.2f}")

    print("\n### TOP 5 ALTERNATIVE CONFIGURATIONS ###\n")
    for i, cfg in enumerate(recommendation["top_alternatives"][:5], 1):
        print(
            f"{i}. {cfg['instance_type']} Gen{cfg['generation']} ({cfg['instance_vcpu']:.0f}C {cfg['instance_memory_gb']:.0f}GB) x {cfg['node_count']}"
        )
        print(
            f"   Total Capacity: {cfg['total_cpu']:.1f} CPU, {cfg['total_memory_gb']:.1f} GB RAM"
        )
        print(f"   Resource Waste: {cfg['avg_waste_percent']:.1f}%%")
        print(
            f"   Costs: ${cfg['total_hourly_cost']:.2f}/hr, ${cfg['total_monthly_cost']:.2f}/mo, ${cfg['total_yearly_cost']:.2f}/yr"
        )
        print()

    print("=" * 80)


app = cyclopts.App(
    help="Analyze Kubernetes cluster resources and recommend baseline nodes"
)


@app.default
def main(
    cluster_name: str,
    region: str,
    *,
    kubeconfig: str | None = None,
    context: str | None = None,
    headroom: int = DEFAULT_HEADROOM_PERCENTAGE,
    json_output: bool = False,
    detailed: bool = False,
    use_actual_usage: bool = False,
) -> None:
    """Analyze Kubernetes cluster resources and recommend baseline nodes.

    Args:
        cluster_name: EKS cluster name.
        region: AWS region.
        kubeconfig: Path to kubeconfig file (default: ~/.kube/config).
        context: Kubernetes context to use (default: use cluster_name).
        headroom: Headroom percentage for autoscaling (default: 20%).
        json_output: Output results as JSON.
        detailed: Show detailed breakdown of each workload.
        use_actual_usage: Use actual observed usage (via metrics-server) instead of declared requests for recommendations.
    """
    try:
        # Resolve context: use explicit context if provided, otherwise use cluster_name
        resolved_context = context or cluster_name

        # Load Kubernetes config
        if kubeconfig:
            config.load_kube_config(config_file=kubeconfig, context=resolved_context)
        else:
            config.load_kube_config(context=resolved_context)

        # Verify we're connected to the right cluster
        print(
            f"Connected to cluster context: {resolved_context}",
            file=sys.stderr,
        )
    except Exception as e:
        print(f"Error loading Kubernetes config: {e}", file=sys.stderr)
        print(
            f"Note: Context defaulted to '{context or cluster_name}'. "
            f"Use --context to specify a different context.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Create API clients
    apps_api = client.AppsV1Api()
    core_api = client.CoreV1Api()
    custom_api = client.CustomObjectsApi()

    # Collect data
    print("Collecting workload metrics...", file=sys.stderr)
    workloads = get_workload_metrics(apps_api)

    # Attempt to collect actual usage metrics from metrics-server
    print("Collecting actual resource usage (via metrics-server)...", file=sys.stderr)
    pod_metrics_by_namespace: dict[str, dict[str, dict[str, ResourceRequest]]] = {}
    namespaces_to_check = {wl.namespace for wl in workloads}

    metrics_available = False
    for namespace in sorted(namespaces_to_check):
        pod_metrics = get_pod_metrics(custom_api, namespace)
        if pod_metrics:
            pod_metrics_by_namespace[namespace] = pod_metrics
            metrics_available = True
            print(
                f"  Found metrics for {len(pod_metrics)} pods in {namespace}",
                file=sys.stderr,
            )

    if metrics_available:
        print("Aggregating pod metrics into workloads...", file=sys.stderr)
        aggregate_pod_metrics(workloads, pod_metrics_by_namespace)
    else:
        print(
            "Warning: No metrics data found. Ensure metrics-server is installed and running.",
            file=sys.stderr,
        )

    print("Collecting node metrics...", file=sys.stderr)
    nodes_info = get_node_metrics(core_api)

    print("Collecting AWS nodegroup information...", file=sys.stderr)
    nodegroups = get_nodegroup_info(region, cluster_name)

    print("Fetching AWS instance types...", file=sys.stderr)
    instance_types = get_instance_types(region)

    if not instance_types:
        print("Error: Could not fetch instance types from AWS", file=sys.stderr)
        sys.exit(1)

    print("Fetching AWS pricing information...", file=sys.stderr)
    get_instance_pricing(region, instance_types)

    # Log pricing statistics and try fallback if needed
    priced_types = sum(1 for it in instance_types.values() if it.price_per_hour > 0)
    print(
        f"Pricing available for {priced_types}/{len(instance_types)} instance types",
        file=sys.stderr,
    )

    if priced_types == 0:
        print(
            "AWS pricing API returned no results. Trying fallback from instances.vantage.sh...",
            file=sys.stderr,
        )
        get_instance_pricing_fallback(region, instance_types)
        priced_types = sum(1 for it in instance_types.values() if it.price_per_hour > 0)

    if priced_types == 0:
        print(
            "Warning: No pricing data found. Cost estimates will be $0.00.",
            file=sys.stderr,
        )

    # Calculate recommendation
    # Use actual usage if available and requested, otherwise fall back to declared requests
    if use_actual_usage and metrics_available:
        total_usage = sum(
            (wl.total_actual_usage_p95() for wl in workloads),
            ResourceRequest(),
        )
        required_cpu = total_usage.cpu_cores
        required_memory_gb = total_usage.memory_bytes / Decimal("1024") ** 3
        print(
            "Using 95th percentile actual usage for recommendations",
            file=sys.stderr,
        )
    else:
        if use_actual_usage and not metrics_available:
            print(
                "Warning: --use-actual-usage requested but metrics not available, falling back to declared requests",
                file=sys.stderr,
            )
        total_requests = sum(
            (wl.total_requests() for wl in workloads),
            ResourceRequest(),
        )
        required_cpu = total_requests.cpu_cores
        required_memory_gb = total_requests.memory_bytes / Decimal("1024") ** 3

    print("Calculating recommendations...", file=sys.stderr)
    recommendation = recommend_baseline_nodes(
        required_cpu, required_memory_gb, instance_types, headroom
    )

    # Perform bin-packing analysis for atomic pod units
    print("Analyzing bin-packing efficiency for atomic pod units...", file=sys.stderr)
    pod_profiles, daemonset_reserved = extract_pod_profiles(workloads)

    # Log DaemonSet resource reservations
    if daemonset_reserved.cpu_cores > 0 or daemonset_reserved.memory_bytes > 0:
        ds_cpu = float(daemonset_reserved.cpu_cores)
        ds_mem = float(daemonset_reserved.memory_bytes / (1024**3))
        print(
            f"  DaemonSet per-node reservation: {ds_cpu:.3f} CPU, {ds_mem:.2f} GB",
            file=sys.stderr,
        )

    bin_packing_results: list[tuple[BinPackingResult, Decimal]] | None = None
    if pod_profiles:
        bin_packing_results = recommend_with_bin_packing(
            pod_profiles, instance_types, headroom, daemonset_reserved
        )
        print(
            f"  Found {len(pod_profiles)} unique pod resource profiles (DaemonSets handled separately)",
            file=sys.stderr,
        )

    # Debug recommended instance type if it has no pricing
    recommended_type = recommendation.get("recommended", {}).get("instance_type")
    if recommended_type and recommended_type in instance_types:
        capacity = instance_types[recommended_type]
        if capacity.price_per_hour == 0:
            print(
                f"⚠️  PRICING ISSUE: Recommended instance type '{recommended_type}' has $0 cost. "
                f"Pricing data may not be available for this instance type in region '{region}'.",
                file=sys.stderr,
            )
            debug_instance_type_pricing(recommended_type, instance_types)

    if json_output:
        # Prepare usage metrics for JSON output
        total_actual_avg = sum(
            (wl.total_actual_usage_avg() for wl in workloads),
            ResourceRequest(),
        )
        total_actual_peak = sum(
            (wl.total_actual_usage_peak() for wl in workloads),
            ResourceRequest(),
        )
        total_actual_p95 = sum(
            (wl.total_actual_usage_p95() for wl in workloads),
            ResourceRequest(),
        )

        result = {
            "cluster": cluster_name,
            "region": region,
            "workload_count": len(workloads),
            "metrics_available": metrics_available,
            "declared_resources": {
                "total_cpu_cores": float(required_cpu),
                "total_memory_gb": float(required_memory_gb),
            },
            "actual_usage": {
                "average_cpu_cores": float(total_actual_avg.cpu_cores),
                "average_memory_gb": float(total_actual_avg.memory_bytes / (1024**3)),
                "peak_cpu_cores": float(total_actual_peak.cpu_cores),
                "peak_memory_gb": float(total_actual_peak.memory_bytes / (1024**3)),
                "p95_cpu_cores": float(total_actual_p95.cpu_cores),
                "p95_memory_gb": float(total_actual_p95.memory_bytes / (1024**3)),
            },
            "nodegroups": [
                {
                    "name": ng.name,
                    "instance_type": ng.instance_type,
                    "node_count": ng.node_count,
                    "capacity": {
                        "cpu_cores": float(ng.total_cpu_cores),
                        "memory_gb": float(ng.total_memory_bytes / (1024**3)),
                    },
                }
                for ng in nodegroups
            ],
            "recommendation": recommendation,
        }
        print(json.dumps(result, indent=2))
    else:
        print_report(
            workloads,
            nodegroups,
            nodes_info,
            recommendation,
            pod_profiles if pod_profiles else None,
            bin_packing_results if bin_packing_results else None,
            headroom,
            daemonset_reserved,
        )

        if detailed:
            print("\n### WORKLOAD DETAILS ###\n")
            for wl in sorted(
                workloads, key=lambda x: (-x.total_requests().cpu_cores, x.name)
            ):
                req = wl.total_requests()
                avg = wl.total_actual_usage_avg()
                peak = wl.total_actual_usage_peak()
                p95 = wl.total_actual_usage_p95()

                print(f"{wl.namespace}/{wl.name} ({wl.kind})")
                print(f"  Replicas: {wl.replicas}")
                print(
                    f"  Declared Requests - CPU: {float(req.cpu_cores):.3f} cores ({int(req.cpu_cores * 1000)}m), Memory: {float(req.memory_bytes / (1024**3)):.2f} GB"
                )

                if avg.cpu_cores > 0 or avg.memory_bytes > 0:
                    print("  Actual Usage:")
                    print(
                        f"    Average - CPU: {float(avg.cpu_cores):.3f} cores, Memory: {float(avg.memory_bytes / (1024**3)):.2f} GB"
                    )
                    print(
                        f"    Peak    - CPU: {float(peak.cpu_cores):.3f} cores, Memory: {float(peak.memory_bytes / (1024**3)):.2f} GB"
                    )
                    print(
                        f"    P95     - CPU: {float(p95.cpu_cores):.3f} cores, Memory: {float(p95.memory_bytes / (1024**3)):.2f} GB"
                    )


if __name__ == "__main__":
    app()
