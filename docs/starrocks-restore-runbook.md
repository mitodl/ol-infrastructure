# StarRocks restore runbook

How to get a lakehouse StarRocks cluster (`applications/starrocks` and
`substructure/starrocks`, stacks `lakehouse.{CI,QA,Production}`) back after its FE
metadata or its shared-data bucket is lost or damaged.

## Status: neither path has been run end to end

Rebuilding from IaC and dbt recovers everything we depend on (see
[What lives where](#what-lives-where)), and its Pulumi and dbt steps are the ones
that built the clusters. The teardown in step 2 and the whole snapshot restore
were written from the StarRocks 4.1 source (`RestoreClusterSnapshotMgr`,
`ClusterSnapshotConfig`, `fe_entrypoint.sh` on `branch-4.1`) and have not been run
against one of our clusters. Run both in CI before relying on them in Production.

## What lives where

The warehouse is not in StarRocks. Every dbt layer is Iceberg in the Glue catalog
on the data-lake buckets (ADR 0012), and StarRocks reads and writes it through the
`ol_data_lake_<env>` external catalogs. Losing StarRocks loses none of it.

What StarRocks holds itself:

| State | Where it lives | Recreated by |
|---|---|---|
| Catalogs, roles, grants, resource groups, query queue, security integrations, `b2b_learner_records` database | FE metadata (`lakehouse-fe-storage-meta-*` PVCs) | `pulumi up` on `substructure/starrocks` (every statement is idempotent or drop-and-create) |
| Native tables and MVs: the `b2b_analytics` and `b2b_learner_records` dbt groups | FE metadata plus tablet files in `ol-starrocks-lakehouse-<env>` | the Dagster `dbt_starrocks` assets in `lakehouse`, which drop and recreate each MV on every build |
| Vault dynamic users | FE metadata | Vault, on the next lease (revoke and reissue the VaultDynamicSecrets) |
| OIDC users | nowhere: ephemeral per connection | the group sync CronJob rewrites the group file |
| Ad-hoc native tables someone created by hand | FE metadata plus tablet files | nothing. Only the snapshot path recovers them |

The automated cluster snapshot (`substructure/starrocks`, hourly) writes an FE
metadata image to `builtin_storage_volume`, which is the shared-data bucket, and
keeps every tablet file that image references out of vacuum. StarRocks keeps one
snapshot and deletes the previous one when the next finishes. The bucket is
versioned with a 14-day noncurrent expiry (`applications/starrocks`), so earlier
images and the tablet files only they referenced stay recoverable for 14 days.

## Check that snapshots are being taken

```sql
ADMIN SHOW AUTOMATED CLUSTER SNAPSHOT;
SELECT snapshot_name, created_time, storage_path
FROM information_schema.cluster_snapshots;
SELECT snapshot_name, state, finished_time, error_message
FROM information_schema.cluster_snapshot_jobs
ORDER BY created_time DESC LIMIT 5;
```

The FE also exports `starrocks_fe_cluster_snapshot_consecutive_failures` and
`starrocks_fe_cluster_snapshot_last_finished_time` (epoch ms, 0 until the first
snapshot finishes). `StarRocksClusterSnapshotFailing` in
`grafana_alerting/metric_rules/starrocks.py` warns when the failure count is
above zero, which usually means the FE cannot write to the bucket, so check the
IRSA role's shared-data bucket policy first.

## Stopping the cluster

Both paths start from a cluster with nothing running. Pausing Dagster is not
enough: a running FE keeps taking snapshots, vacuuming and compacting the same
service-id prefix, and the operator recreates any pod or StatefulSet you delete.
All commands are in namespace `starrocks`, on the environment's data cluster.

1. Pause the `lakehouse` dbt schedules and sensors in Dagster.
2. Stop the operator, so nothing reconciles while you work:
   `kubectl -n starrocks scale deploy/starrocks-operator-operator --replicas=0`.
3. Scale both StatefulSets to zero and wait for every pod to go:
   `kubectl -n starrocks scale sts/lakehouse-starrocks-fe sts/lakehouse-starrocks-cn --replicas=0`,
   then `kubectl -n starrocks get pods` until no FE or CN pod is left. The CN
   HPA scales the `StarRocksCluster` resource, not the StatefulSet, so with the
   operator stopped it cannot bring the CNs back.

The cluster comes back when you scale the operator to 1 again. It re-renders both
StatefulSets from the `StarRocksCluster` resource.

## Setting root's password on an empty FE

The FE and CN containers log in as root with `MYSQL_PWD`, from Secret
`lakehouse-starrocks-root-password`. That covers follower joins and CN
registration in the entrypoints, and the substructure stack reads the same
password. A FE that starts with empty metadata has a root with no password, and
the chart's `initPassword` job ran only at install. So as soon as FE 0 is up on
empty metadata, before any follower or CN starts, set it:

```bash
kubectl -n starrocks exec lakehouse-starrocks-fe-0 -c fe -- \
  sh -c 'pw="$MYSQL_PWD"; env -u MYSQL_PWD mysql -h127.0.0.1 -P9030 -uroot \
    -e "SET PASSWORD FOR root = PASSWORD(\"$pw\")"'
```

A snapshot restore brings back root's password as of the snapshot. Run the same
command if the Secret has changed since.

## Path 1: rebuild

Use this when the FE metadata is gone or untrustworthy and nothing hand-made in
`default_catalog` needs to survive. Nothing in Iceberg is touched.

1. [Stop the cluster](#stopping-the-cluster).
2. Delete every FE PVC (`lakehouse-fe-storage-meta-*` and
   `lakehouse-fe-storage-log-*`) and wait until `kubectl get pvc` no longer lists
   them. A follower that keeps its old meta skips the entrypoint's `--helper`
   join and comes back with the old cluster's state (`fe_entrypoint.sh`,
   `start_fe_with_meta`). Do not `pulumi destroy` the applications stack: it
   owns the shared-data bucket.
3. Set `starrocks:fe_config.replicas` to 1 in the applications stack config,
   `pulumi up` on `applications/starrocks`, then start the operator again
   (`--replicas=1`). FE 0 starts on new, empty volumes.
4. [Set root's password](#setting-roots-password-on-an-empty-fe).
5. Delete the CN pods. A CN registers with the FE only when its container
   starts (`cn_entrypoint.sh`), and the ones that started before step 4 could
   not log in. `SHOW COMPUTE NODES` should list them all as alive.
6. A new FE writes under a new service id in the same bucket, as QA's bucket
   already shows (two prefixes). Leave the old prefix until the rebuild is
   verified, then delete it.
7. Set the FE replicas back to the stack's configured count (CI runs one) and
   `pulumi up` on `applications/starrocks`. The followers start on empty volumes
   and join through `--helper`.
8. `pulumi up` on `substructure/starrocks`. Its Command resources are already
   "created" in state and their triggers (where they have any) hash SQL that has
   not changed, so they will not run against the empty FE on their own. Remove
   them from state first, so the next `up` creates them:
   `pulumi state delete --target-dependents <urn>` on each
   `command:local:Command` that is still in state, starting with
   `starrocks-<env>-roles-setup`, which the grant and resource-group Commands
   depend on. Check `pulumi stack --show-urns` afterwards for any left over.
   `--target-dependents` also drops non-Command resources downstream of them
   from state: the keycloak group sync's `OLVaultK8SSecret` and CronJob
   (`group_sync_job.py`), which depend on the group-provider Command. They keep
   running in the cluster. If the next `up` fails on them with AlreadyExists,
   delete those objects in namespace `starrocks` and run `up` again; it
   recreates them.
9. Revoke and reissue the VaultDynamicSecrets in the consumers' namespaces
   (Superset, ol-analytics-api, OpenMetadata, mit-learn) so they get users that
   exist in the new FE.
10. Materialize the `b2b_analytics` and `b2b_learner_records` assets in the
    `lakehouse` code location, then the `starrocks_mv_refresh` asset.
11. Unpause the schedules. Confirm Superset's StarRocks database connects and
    the B2B dashboards in MIT Learn load.

## Path 2: restore from the cluster snapshot

Use this when hand-made native objects or the exact FE state must come back. It
restores the cluster as of the last snapshot, at most one hour old.

1. Find the service id while an FE is still up:
   `SELECT storage_path FROM information_schema.cluster_snapshots`. If none is
   up, take the prefix in `s3://ol-starrocks-lakehouse-<env>/` holding the
   newest `<service_id>/meta/image/automated_cluster_snapshot_<ms>/`.
2. [Stop the cluster](#stopping-the-cluster). Nothing may touch the bucket
   between here and step 6.
3. If the snapshot you need was already purged (the mistake was snapshotted
   over), list `aws s3api list-object-versions --prefix <service_id>/` and
   restore as current versions the older image directory and every tablet file
   deleted after that image's timestamp. The FE picks the newest image directory
   that has a `snapshot_meta.json`, so remove newer image directories too.
4. Delete the meta and log PVCs of every FE except `lakehouse-starrocks-fe-0`
   and wait until they are gone. FE 0's can stay: the restore deletes its local
   image and BDB directories before it downloads the snapshot.
5. Write `cluster_snapshot.yaml`. Without its `cluster_snapshot` section the FE
   restores from its own local image, which is no use when the meta PVC is lost.
   A path ending in `/meta` makes the FE take the newest complete snapshot under
   it. The volume name has to match the snapshot's. `frontends: []` drops the
   snapshot's follower records (`RestoreClusterSnapshotMgr.updateFrontends`
   leaves them when the key is absent), so FE 0 comes up as a group of one.
   `compute_nodes` is left out: the CN pods keep their host names, so the
   snapshot's CN records still match them.

   ```yaml
   cluster_snapshot:
     cluster_snapshot_path: s3://ol-starrocks-lakehouse-<env>/<service_id>/meta
     storage_volume_name: builtin_storage_volume
   frontends: []
   storage_volumes:
     - name: builtin_storage_volume
       type: S3
       location: s3://ol-starrocks-lakehouse-<env>
       properties:
         - key: aws.s3.region
           value: us-east-1
         - key: aws.s3.use_aws_sdk_default_behavior
           value: "true"
   ```

6. Start FE 0 in restore mode. The FE reads
   `/opt/starrocks/fe/conf/cluster_snapshot.yaml` and restores when started with
   `--cluster_snapshot` or with `RESTORE_CLUSTER_SNAPSHOT=true` in its
   environment. `fe_entrypoint.sh` passes neither and links into `conf/` only the
   files of the operator's FE ConfigMap. So, for this one start, put the yaml in a
   ConfigMap added to `starrocksFESpec.configMaps` and extend the FE entrypoint
   script (the Gravitino block in `applications/starrocks` shows the shape) to
   copy it into `/opt/starrocks/fe/conf/` and export
   `RESTORE_CLUSTER_SNAPSHOT=true` before it execs `fe_entrypoint.sh`. Set
   `starrocks:fe_config.replicas` to 1 with it, `pulumi up` on
   `applications/starrocks`, then start the operator again.
7. When FE 0 is up and `SHOW FRONTENDS` lists only it, remove the script change
   and the ConfigMap and `pulumi up` again. Left in place, every restart restores
   again. Set root's password if the Secret changed after the snapshot.
8. Bring authorization back to now before anyone queries. The restored FE has
   the users, grants, catalogs and settings of the snapshot, and Pulumi state
   was not rewound with it, so a plain `pulumi up` changes nothing. Run Path 1
   step 8 (Command state reset, then `pulumi up` on `substructure/starrocks`).
   Then drop every restored Vault user (`SHOW USERS`, names from the
   `database-starrocks` roles' username template) with no live lease under
   `vault list sys/leases/lookup/database-starrocks/creds/<role>`, and do Path 1
   step 9. Consumers can reach FE 0 from step 6 on, and queries run once the CNs
   register, so do this step straight away. Writers stay paused until step 10.
9. Delete the CN pods so their local caches start empty, then set the FE
   replicas back to the stack's configured count. The new followers start with
   empty PVCs (step 4), so they join through the entrypoint's `--helper` path.
10. Check `SHOW DATABASES`, the b2b MVs' row counts and `SHOW RESOURCE GROUPS`,
    then unpause the schedules. Restoring into a different bucket or cluster
    needs every file under the old storage path copied first, plus a
    `compute_nodes` section from StarRocks' `conf/cluster_snapshot.yaml`
    template.

## Limitations

- Cluster Snapshot is a Beta feature in StarRocks 4.1 and keeps one snapshot.
- `ADMIN SET AUTOMATED CLUSTER SNAPSHOT OFF` deletes that snapshot from S3. The
  Pulumi Command that turns snapshots on has no delete step for this reason, so
  removing it from the stack leaves snapshots on. Only bucket versioning brings
  a snapshot back after an OFF.
- Ad-hoc native tables created between the last snapshot and the failure are
  lost on either path.
- A snapshot is a point-in-time copy of the shared-data bucket's own view. It is
  not a backup against losing the bucket. Versioning covers deleted objects for
  14 days. Deleting the bucket itself is blocked only by its non-empty state.
