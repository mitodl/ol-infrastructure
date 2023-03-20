import boto3
import itertools
import csv

ec2 = boto3.client("ec2")
rds = boto3.client("rds")
cache = boto3.client("elasticache")
groups = ec2.describe_security_groups()
ec2_instances = ec2.describe_instances()
rds_instances = rds.describe_db_instances()
cache_clusters = cache.describe_cache_clusters()

active_groups = []
for instance in ec2_instances["Reservations"]:
    active_groups.extend([box["SecurityGroups"] for box in instance["Instances"]])

flat_groups = itertools.chain.from_iterable(active_groups)
active_group_ids = {(group["GroupId"], group["GroupName"]) for group in flat_groups}
all_group_ids = {
    (group["GroupId"], group["GroupName"]) for group in groups["SecurityGroups"]
}

stale_groups = all_group_ids - active_group_ids
with open("stale_security_groups.csv", "w") as stale_csv:
    writer = csv.DictWriter(stale_csv, fieldnames=("Group ID", "Group Name"))
    writer.writeheader()
    for group in stale_groups:
        writer.writerow({"Group ID": group[0], "Group Name": group[1]})
