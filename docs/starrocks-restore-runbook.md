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

The FE also exports `cluster_snapshot_consecutive_failures` and
`cluster_snapshot_last_finished_time` (epoch ms). A failure count above zero
means the FE cannot write to the bucket, so check the IRSA role's shared-data
bucket policy first.

## Path 1: rebuild

Use this when the FE metadata is gone or untrustworthy and nothing hand-made in
`default_catalog` needs to survive. Nothing in Iceberg is touched.

1. Stop writers: pause the `lakehouse` dbt schedules and sensors in Dagster.
2. Give the FE empty metadata. Set `starrocks:fe_config.replicas` to 1 in the
   applications stack config and `pulumi up`, then delete pod
   `lakehouse-starrocks-fe-0` together with its PVCs
   `lakehouse-fe-storage-meta-lakehouse-starrocks-fe-0` and
   `lakehouse-fe-storage-log-lakehouse-starrocks-fe-0` in namespace `starrocks`.
   The StatefulSet recreates the pod on new volumes and the FE starts with no
   meta. Delete the other FE PVCs left behind by the scale-down.
   Do not `pulumi destroy` the applications stack: it owns the shared-data bucket.
3. A new FE writes under a new service id in the same bucket, as QA's bucket
   already shows (two prefixes). Leave the old prefix until the rebuild is
   verified, then delete it.
4. Set the FE replicas back to 3 and `pulumi up` on `applications/starrocks`.
   Then `pulumi up` on `substructure/starrocks`. Most of its Command resources
   have no triggers and are already "created" in state, so they will not run
   against the empty FE on their own: remove them from state first
   (`pulumi state delete <urn>` for each `command:local:Command`), and the next
   `up` creates them.
5. Revoke and reissue the VaultDynamicSecrets in the consumers' namespaces
   (Superset, ol-analytics-api, OpenMetadata, mit-learn) so they get users that
   exist in the new FE.
6. Materialize the `b2b_analytics` and `b2b_learner_records` assets in the
   `lakehouse` code location, then the `starrocks_mv_refresh` asset.
7. Unpause the schedules. Confirm Superset's StarRocks database connects and the
   B2B dashboards in MIT Learn load.

## Path 2: restore from the cluster snapshot

Use this when hand-made native objects or the exact FE state must come back. It
restores the cluster as of the last snapshot, at most one hour old.

1. Find the service id: `SELECT storage_path FROM
   information_schema.cluster_snapshots` on a running FE, or the newest
   `<service_id>/meta/image/automated_cluster_snapshot_<ms>/` in
   `s3://ol-starrocks-lakehouse-<env>/` when the FE is gone and more than one
   prefix exists.
2. If the snapshot you need was already purged (the mistake was snapshotted
   over), list `aws s3api list-object-versions --prefix <service_id>/` and
   restore as current versions the older image directory and every tablet file
   deleted after that image's timestamp. The FE picks the newest image directory
   that has a `snapshot_meta.json`, so remove newer image directories too.
3. Stop writers as in Path 1, and set the FE replicas to 1 as in Path 1 step 2.
4. Write `cluster_snapshot.yaml`. Without its `cluster_snapshot` section the FE
   restores from its own local image, which is no use when the meta PVC is lost.
   A path ending in `/meta` makes the FE take the newest complete snapshot under
   it. The volume name has to match the snapshot's.

   ```yaml
   cluster_snapshot:
     cluster_snapshot_path: s3://ol-starrocks-lakehouse-<env>/<service_id>/meta
     storage_volume_name: builtin_storage_volume
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

5. Start FE 0 in restore mode. The FE reads
   `/opt/starrocks/fe/conf/cluster_snapshot.yaml` and restores when started with
   `--cluster_snapshot` or with `RESTORE_CLUSTER_SNAPSHOT=true` in its
   environment. `fe_entrypoint.sh` passes neither and links into `conf/` only the
   files of the operator's FE ConfigMap. So, for this one start, put the yaml in a
   ConfigMap added to `starrocksFESpec.configMaps` and extend the FE entrypoint
   script (the Gravitino block in `applications/starrocks` shows the shape) to
   copy it into `/opt/starrocks/fe/conf/` and export
   `RESTORE_CLUSTER_SNAPSHOT=true` before it execs `fe_entrypoint.sh`. The FE
   deletes its local image and BDB directories itself before downloading.
6. When FE 0 is up and `SHOW FRONTENDS` lists only it, remove the script change
   and the ConfigMap. Left in place, every restart restores again.
7. Recreate the CN pods so their local caches start empty, then set the FE
   replicas back to 3. New followers join through the entrypoint's `--helper`
   path.
8. Check `SHOW DATABASES`, the b2b MVs' row counts and `SHOW RESOURCE GROUPS`,
   then `pulumi up` on `substructure/starrocks` to bring the FE back to what the
   code says. Restoring into a different bucket or cluster needs every file under
   the old storage path copied first, plus the `frontends` and `compute_nodes`
   sections of StarRocks' `conf/cluster_snapshot.yaml` template.

## Limitations

- Cluster Snapshot is a Beta feature in StarRocks 4.1 and keeps one snapshot.
- Ad-hoc native tables created between the last snapshot and the failure are
  lost on either path.
- A snapshot is a point-in-time copy of the shared-data bucket's own view. It is
  not a backup against losing the bucket. Versioning covers deleted objects for
  14 days. Deleting the bucket itself is blocked only by its non-empty state.
