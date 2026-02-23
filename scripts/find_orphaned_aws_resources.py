#!/usr/bin/env python
"""
Identify orphaned AWS resources candidates for deletion.

Detects:
- Unattached EBS volumes
- Unused Elastic IPs
- Orphaned network interfaces (not attached to instances)
- Unused RDS snapshots (older than retention period)
- Stale security groups (not in use)
- Unused subnets (no resources, no default route)
- Orphaned AMIs (not used by launch templates or autoscaling groups)

Usage:
    python find_orphaned_aws_resources.py --region us-east-1 --days 30

Output:
    orphaned_resources_YYYY-MM-DD.csv
"""

import csv
import itertools
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
import cyclopts


def find_orphaned_ebs_volumes(ec2: Any) -> list[dict[str, Any]]:
    """Find unattached EBS volumes older than 30 days."""
    orphaned = []
    response = ec2.describe_volumes()

    for volume in response.get("Volumes", []):
        # Check if unattached
        if not volume["Attachments"]:
            # Check age (created_time is in UTC)
            created = volume["CreateTime"].replace(tzinfo=UTC)
            age_days = (datetime.now(UTC) - created).days

            orphaned.append(
                {
                    "Resource Type": "EBS Volume",
                    "Resource ID": volume["VolumeId"],
                    "Region": volume["AvailabilityZone"],
                    "Status": volume["State"],
                    "Size (GB)": volume["Size"],
                    "Created": volume["CreateTime"].isoformat(),
                    "Age (days)": age_days,
                    "Tags": ", ".join(
                        [
                            f"{tag['Key']}={tag['Value']}"
                            for tag in volume.get("Tags", [])
                        ]
                    ),
                }
            )

    return orphaned


def find_orphaned_elastic_ips(ec2: Any) -> list[dict[str, Any]]:
    """Find Elastic IPs not associated with instances."""
    response = ec2.describe_addresses()

    return [
        {
            "Resource Type": "Elastic IP",
            "Resource ID": address["PublicIp"],
            "Allocation ID": address.get("AllocationId", "N/A"),
            "Associated Instance": address.get("InstanceId", "None"),
            "Domain": address.get("Domain", "N/A"),
        }
        for address in response.get("Addresses", [])
        if "InstanceId" not in address or not address["InstanceId"]
    ]


def find_orphaned_network_interfaces(ec2: Any) -> list[dict[str, Any]]:
    """Find network interfaces not attached to instances."""
    response = ec2.describe_network_interfaces()

    return [
        {
            "Resource Type": "Network Interface",
            "Resource ID": eni["NetworkInterfaceId"],
            "Status": eni["Status"],
            "Subnet": eni["SubnetId"],
            "VPC": eni["VpcId"],
            "Tags": ", ".join(
                [f"{tag['Key']}={tag['Value']}" for tag in eni.get("Tags", [])]
            ),
        }
        for eni in response.get("NetworkInterfaces", [])
        if eni["Status"] == "available" and not eni.get("Attachment")
    ]


def find_orphaned_security_groups(ec2: Any) -> list[dict[str, Any]]:
    """Find security groups not in use by any resources."""
    orphaned = []

    # Get all security groups
    all_groups_response = ec2.describe_security_groups()
    all_groups = {
        (sg["GroupId"], sg["GroupName"], sg.get("VpcId")): sg
        for sg in all_groups_response["SecurityGroups"]
    }

    # Collect active security groups from various resources
    active_groups = set()

    # From EC2 instances
    instances_response = ec2.describe_instances()
    for reservation in instances_response.get("Reservations", []):
        for instance in reservation["Instances"]:
            for sg in instance.get("SecurityGroups", []):
                if "GroupId" in sg:
                    active_groups.add(sg["GroupId"])

    # From RDS instances
    try:
        rds = boto3.client("rds")
        db_response = rds.describe_db_instances()
        for db in db_response.get("DBInstances", []):
            for sg in db.get("VpcSecurityGroups", []):
                if "VpcSecurityGroupId" in sg:
                    active_groups.add(sg["VpcSecurityGroupId"])
    except Exception:  # noqa: S110
        # Silently skip RDS if inaccessible
        pass

    # From ElastiCache clusters
    try:
        cache = boto3.client("elasticache")
        cache_response = cache.describe_cache_clusters()
        for cluster in cache_response.get("CacheClusters", []):
            for sg in cluster.get("SecurityGroups", []):
                if "SecurityGroupId" in sg:
                    active_groups.add(sg["SecurityGroupId"])
    except Exception:  # noqa: S110
        # Silently skip ElastiCache if inaccessible
        pass

    # From Network Interfaces
    eni_response = ec2.describe_network_interfaces()
    for eni in eni_response.get("NetworkInterfaces", []):
        for sg in eni.get("Groups", []):
            active_groups.add(sg["GroupId"])

    # Find stale groups (excluding default)
    for (group_id, group_name, vpc_id), sg_data in all_groups.items():
        if group_id not in active_groups and group_name != "default":
            orphaned.append(
                {
                    "Resource Type": "Security Group",
                    "Resource ID": group_id,
                    "Group Name": group_name,
                    "VPC": vpc_id or "EC2-Classic",
                    "Ingress Rules": len(sg_data.get("IpPermissions", [])),
                    "Egress Rules": len(sg_data.get("IpPermissionsEgress", [])),
                }
            )

    return orphaned


def find_orphaned_rds_snapshots(rds: Any, days_old: int = 90) -> list[dict[str, Any]]:
    """Find RDS snapshots older than retention period not associated with DB."""
    response = rds.describe_db_snapshots()
    orphaned = []

    for snapshot in response.get("DBSnapshots", []):
        # Check if snapshot is manual (not automated)
        if snapshot["SnapshotType"] == "manual":
            created = snapshot["SnapshotCreateTime"].replace(tzinfo=UTC)
            age_days = (datetime.now(UTC) - created).days

            if age_days > days_old:
                orphaned.append(
                    {
                        "Resource Type": "RDS Snapshot (Manual)",
                        "Resource ID": snapshot["DBSnapshotIdentifier"],
                        "DB Instance": snapshot.get("DBInstanceIdentifier", "Deleted"),
                        "Created": snapshot["SnapshotCreateTime"].isoformat(),
                        "Age (days)": age_days,
                        "Size (GB)": snapshot["AllocatedStorage"],
                        "Status": snapshot["Status"],
                    }
                )

    return orphaned


def find_orphaned_subnets(ec2: Any) -> list[dict[str, Any]]:
    """Find subnets with no running resources and no explicit route tables."""
    orphaned = []
    subnets_response = ec2.describe_subnets()

    for subnet in subnets_response.get("Subnets", []):
        subnet_id = subnet["SubnetId"]

        # Check for running instances in this subnet
        instances_response = ec2.describe_instances(
            Filters=[
                {"Name": "subnet-id", "Values": [subnet_id]},
                {"Name": "instance-state-name", "Values": ["running"]},
            ]
        )

        has_instances = False
        for reservation in instances_response.get("Reservations", []):
            if reservation["Instances"]:
                has_instances = True
                break

        if not has_instances:
            # Check if subnet has explicit route table association
            route_tables_response = ec2.describe_route_tables(
                Filters=[{"Name": "association.subnet-id", "Values": [subnet_id]}]
            )

            explicit_rt = len(route_tables_response.get("RouteTables", [])) > 0

            if not explicit_rt:
                orphaned.append(
                    {
                        "Resource Type": "Subnet",
                        "Resource ID": subnet_id,
                        "VPC": subnet["VpcId"],
                        "CIDR": subnet["CidrBlock"],
                        "Availability Zone": subnet["AvailabilityZone"],
                        "Available IPs": subnet["AvailableIpAddressCount"],
                    }
                )

    return orphaned


def find_orphaned_amis(ec2: Any) -> list[dict[str, Any]]:
    """Find AMIs not used by launch templates or autoscaling groups."""
    orphaned = []

    # Get all owned AMIs (exclude public/community images)
    amis_response = ec2.describe_images(Owners=["self"])
    all_amis = {ami["ImageId"]: ami for ami in amis_response.get("Images", [])}

    # Collect active AMI IDs from launch template versions
    active_amis = set()

    # From launch templates
    try:
        lt_response = ec2.describe_launch_templates()
        for lt in lt_response.get("LaunchTemplates", []):
            lt_id = lt["LaunchTemplateId"]
            # Get all versions of this launch template
            try:
                versions_response = ec2.describe_launch_template_versions(
                    LaunchTemplateId=lt_id
                )
                for version in versions_response.get("LaunchTemplateVersions", []):
                    ami_id = version.get("LaunchTemplateData", {}).get("ImageId")
                    if ami_id:
                        active_amis.add(ami_id)
            except Exception:  # noqa: S110
                # Skip if template version list inaccessible
                pass
    except Exception:  # noqa: S110
        # Skip if launch templates inaccessible
        pass

    # From autoscaling groups
    try:
        asg = boto3.client("autoscaling", region_name=ec2.meta.region_name)
        asgs_response = asg.describe_auto_scaling_groups()
        for group in asgs_response.get("AutoScalingGroups", []):
            # Check launch template
            lt_spec = group.get("LaunchTemplate")
            if lt_spec:
                lt_id = lt_spec.get("LaunchTemplateId")
                if lt_id:
                    try:
                        versions_response = ec2.describe_launch_template_versions(
                            LaunchTemplateId=lt_id
                        )
                        for version in versions_response.get(
                            "LaunchTemplateVersions", []
                        ):
                            ami_id = version.get("LaunchTemplateData", {}).get(
                                "ImageId"
                            )
                            if ami_id:
                                active_amis.add(ami_id)
                    except Exception:  # noqa: S110
                        # Skip if template inaccessible
                        pass

            # Check launch configuration (legacy)
            lc_name = group.get("LaunchConfigurationName")
            if lc_name:
                try:
                    lcs_response = asg.describe_launch_configurations(
                        LaunchConfigurationNames=[lc_name]
                    )
                    for lc in lcs_response.get("LaunchConfigurations", []):
                        ami_id = lc.get("ImageId")
                        if ami_id:
                            active_amis.add(ami_id)
                except Exception:  # noqa: S110
                    # Skip if configuration inaccessible
                    pass
    except Exception:  # noqa: S110
        # Silently skip autoscaling if inaccessible
        pass

    # Find orphaned AMIs
    for ami_id, ami_data in all_amis.items():
        if ami_id not in active_amis:
            created = ami_data["CreationDate"]
            # Parse ISO format datetime string
            if isinstance(created, str):
                created_dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                created_dt = created.replace(tzinfo=UTC)

            age_days = (datetime.now(UTC) - created_dt).days

            orphaned.append(
                {
                    "Resource Type": "AMI",
                    "Resource ID": ami_id,
                    "Name": ami_data.get("Name", "N/A"),
                    "Created": ami_data["CreationDate"],
                    "Age (days)": age_days,
                    "State": ami_data.get("State", "unknown"),
                    "Root Device": ami_data.get("RootDeviceName", "N/A"),
                    "Size (GB)": sum(
                        bdm.get("Ebs", {}).get("VolumeSize", 0)
                        for bdm in ami_data.get("BlockDeviceMappings", [])
                    ),
                    "Tags": ", ".join(
                        [
                            f"{tag['Key']}={tag['Value']}"
                            for tag in ami_data.get("Tags", [])
                        ]
                    ),
                }
            )

    return orphaned


def main(
    region: str = "us-east-1",
    days: int = 90,
    output: str | None = None,
) -> None:
    """
    Find orphaned AWS resources candidates for deletion.

    Args:
        region: AWS region to scan (default: us-east-1)
        days: Age threshold in days for RDS snapshots (default: 90)
        output: Output file path (default: orphaned_resources_YYYY-MM-DD.csv)
    """
    # Initialize AWS clients
    ec2 = boto3.client("ec2", region_name=region)
    rds = boto3.client("rds", region_name=region)

    print(f"Scanning {region} for orphaned resources...")

    # Collect all orphaned resources
    all_orphaned: list[dict[str, Any]] = []

    print("  - Checking EBS volumes...")
    all_orphaned.extend(find_orphaned_ebs_volumes(ec2))

    print("  - Checking Elastic IPs...")
    all_orphaned.extend(find_orphaned_elastic_ips(ec2))

    print("  - Checking network interfaces...")
    all_orphaned.extend(find_orphaned_network_interfaces(ec2))

    print("  - Checking security groups...")
    all_orphaned.extend(find_orphaned_security_groups(ec2))

    print("  - Checking RDS snapshots...")
    all_orphaned.extend(find_orphaned_rds_snapshots(rds, days))

    print("  - Checking subnets...")
    all_orphaned.extend(find_orphaned_subnets(ec2))

    print("  - Checking AMIs...")
    all_orphaned.extend(find_orphaned_amis(ec2))

    # Generate output filename
    if output:
        output_file = Path(output)
    else:
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        output_file = Path(f"orphaned_resources_{date_str}.csv")

    # Write results to CSV
    if all_orphaned:
        # Get all unique fieldnames across all resources
        fieldnames = sorted(
            set(itertools.chain.from_iterable(r.keys() for r in all_orphaned))
        )

        with output_file.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_orphaned)

        print(f"\nFound {len(all_orphaned)} orphaned resources")
        print(f"Results written to: {output_file.absolute()}")
    else:
        print("\nNo orphaned resources found")


if __name__ == "__main__":
    app = cyclopts.App(default_command=main)
    app()
