#!/usr/bin/env python3
# ruff: noqa: T201
"""Taint and drain all nodes in an old EKS nodegroup, then optionally delete its ASG.

Prereqs:
- AWS credentials with permissions to describe/update Auto Scaling Groups and EC2.

- Kubeconfig set to point to the cluster (export KUBECONFIG or run `aws eks
  update-kubeconfig`).

- Python deps: boto3, kubernetes.

Usage:
  python scripts/eks/drain_old_nodegroup.py \
    --asg-name <OLD_ASG_NAME> \
    [--taint-key ol.mit.edu/decommission] \
    [--taint-value true] \
    [--timeout-seconds 1200] \
    [--scale-to-zero] \
    [--delete-asg]

Notes:
- If you pass --delete-asg, the ASG will be deleted in AWS, which will drift from Pulumi
  state until you remove the old nodegroup from code and run `pulumi up`.

"""

from __future__ import annotations

import argparse
import time
from collections.abc import Iterable

import boto3
from botocore.exceptions import ClientError
from kubernetes import client, config
from kubernetes.client import V1Pod
from kubernetes.client.exceptions import ApiException


def _get_asg_instance_ids(asg_name: str) -> list[str]:
    asg = boto3.client("autoscaling")
    resp = asg.describe_auto_scaling_groups(AutoScalingGroupNames=[asg_name])
    groups = resp.get("AutoScalingGroups", [])
    if not groups:
        msg = f"ASG not found: {asg_name}"
        raise SystemExit(msg)
    return [inst["InstanceId"] for inst in groups[0].get("Instances", [])]


def _instance_ids_to_node_names(instance_ids: Iterable[str]) -> list[str]:
    ec2 = boto3.client("ec2")
    if not instance_ids:
        return []
    resp = ec2.describe_instances(InstanceIds=list(instance_ids))
    names: list[str] = []
    for r in resp.get("Reservations", []):
        for i in r.get("Instances", []):
            # EKS nodes typically use PrivateDnsName as the kube node name
            if i.get("PrivateDnsName"):
                names.append(i["PrivateDnsName"])  # noqa: PERF401
    return names


def _cordon_and_taint_node(
    core: client.CoreV1Api, node_name: str, taint_key: str, taint_value: str
) -> None:
    # Cordon
    core.patch_node(node_name, {"spec": {"unschedulable": True}})
    # Taint
    patch = {
        "spec": {
            "taints": [
                {
                    "key": taint_key,
                    "value": taint_value,
                    "effect": "NoSchedule",
                }
            ]
        }
    }
    try:
        core.patch_node(node_name, patch)
    except ApiException as exc:  # Best-effort (node may already have taints)
        if exc.status not in (200, 201):
            raise


def _is_daemon_pod(pod: V1Pod) -> bool:
    if not pod.metadata or not pod.metadata.owner_references:
        return False
    return any(owner.kind == "DaemonSet" for owner in pod.metadata.owner_references)


def _is_mirror_pod(pod: V1Pod) -> bool:
    annotations = (pod.metadata and pod.metadata.annotations) or {}
    return "kubernetes.io/config.mirror" in annotations


def _evict_pod(policy_api: client.PolicyV1Api, pod: V1Pod, grace_period_seconds=60):
    body = client.V1Eviction(
        delete_options=client.V1DeleteOptions(
            grace_period_seconds=grace_period_seconds
        ),
        metadata=client.V1ObjectMeta(
            name=pod.metadata.name, namespace=pod.metadata.namespace
        ),
    )
    try:
        policy_api.create_namespaced_pod_eviction(
            name=pod.metadata.name, namespace=pod.metadata.namespace, body=body
        )
    except ApiException as exc:
        if exc.status not in (200, 201, 202, 404):
            raise


def _drain_node(
    core: client.CoreV1Api,
    policy_api: client.PolicyV1Api,
    node_name: str,
    timeout_seconds: int,
):
    # Evict all non-daemon, non-mirror pods
    field_selector = f"spec.nodeName={node_name}"
    pods = core.list_pod_for_all_namespaces(field_selector=field_selector).items
    for pod in pods:
        if _is_daemon_pod(pod) or _is_mirror_pod(pod):
            continue
        _evict_pod(policy_api, pod)

    # Wait until pods are gone
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        pods = core.list_pod_for_all_namespaces(field_selector=field_selector).items
        remaining = [p for p in pods if not (_is_daemon_pod(p) or _is_mirror_pod(p))]
        if not remaining:
            return
        time.sleep(5)
    msg = f"Timeout draining node {node_name}"
    raise TimeoutError(msg)


def _scale_asg_to_zero(asg_name: str):
    asg = boto3.client("autoscaling")
    asg.update_auto_scaling_group(
        AutoScalingGroupName=asg_name, MinSize=0, MaxSize=0, DesiredCapacity=0
    )


def _delete_asg(asg_name: str):
    asg = boto3.client("autoscaling")
    try:
        asg.delete_auto_scaling_group(AutoScalingGroupName=asg_name, ForceDelete=True)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ResourceInUse":
            # Try scaling to zero and retry delete shortly after
            _scale_asg_to_zero(asg_name)
            time.sleep(10)
            asg.delete_auto_scaling_group(
                AutoScalingGroupName=asg_name, ForceDelete=True
            )
        else:
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asg-name", required=True, help="Old nodegroup ASG name")
    parser.add_argument(
        "--taint-key",
        default="ol.mit.edu/decommission",
        help="Taint key to apply to old nodes",
    )
    parser.add_argument("--taint-value", default="true", help="Taint value")
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=20 * 60,
        help="Drain timeout per node (seconds)",
    )
    parser.add_argument(
        "--scale-to-zero",
        action="store_true",
        help="After drain, scale the old ASG to zero",
    )
    parser.add_argument(
        "--delete-asg",
        action="store_true",
        help="After drain, delete the old ASG (will drift from Pulumi state)",
    )
    args = parser.parse_args()

    # Build clients
    config.load_kube_config()
    core = client.CoreV1Api()
    policy_api = client.PolicyV1Api()

    instance_ids = _get_asg_instance_ids(args.asg_name)
    node_names = _instance_ids_to_node_names(instance_ids)
    if not node_names:
        print("No instances/nodes found in ASG; nothing to do.")
        return

    # Cordon/taint and drain nodes
    for node_name in node_names:
        print(f"Cordoning and tainting {node_name}…")
        _cordon_and_taint_node(core, node_name, args.taint_key, args.taint_value)
    for node_name in node_names:
        print(f"Draining {node_name}…")
        _drain_node(core, policy_api, node_name, args.timeout_seconds)

    # Optionally scale to zero or delete
    if args.delete_asg:
        print(f"Deleting ASG {args.asg_name}…")
        _delete_asg(args.asg_name)
    elif args.scale_to_zero:
        print(f"Scaling ASG {args.asg_name} to zero…")
        _scale_asg_to_zero(args.asg_name)

    print("Done.")


if __name__ == "__main__":
    main()
