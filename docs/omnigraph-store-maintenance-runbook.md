# omnigraph store maintenance runbook

What keeps the witan graph store from degrading, what runs on a schedule, and
what to do by hand when it drifts.

## What runs on its own

Two CronJobs in the `omnigraph` namespace, both defined in
`src/ol_infrastructure/applications/omnigraph/maintenance.py`:

| CronJob | Default schedule | What it does | Destructive |
|---|---|---|---|
| `omnigraph-optimize` | `20 3 * * *` (nightly) | Compacts small Lance fragments in every table of every graph | No |
| `omnigraph-cleanup` | `20 4 * * 0` (Sundays) | Removes Lance versions older than 30d | **Yes** |

Both run the `omnigraph` CLI baked into the `omnigraph-server` image, as the
`omnigraph-server` IRSA service account, **against the S3 store directly** —
never through the server. That is not a shortcut: `optimize`, `cleanup` and
`repair` are direct-storage commands and reject `--server` outright.

Running compaction while the server is serving is safe. omnigraph's publisher
uses compare-and-swap, so a maintenance write that loses a race refreshes and
retries rather than corrupting anything.

Per-environment overrides (`pulumi config set omnigraph:<key>`):

- `optimize_schedule`, `cleanup_schedule` — cron expressions, in UTC.
- `cleanup_older_than` — a Go-style duration (`30d`, `72h`).

Keep the two schedules apart. Each is `concurrencyPolicy: Forbid` against
*itself*, but Kubernetes cannot express "forbid against that other CronJob", so
only the schedule gap stops cleanup from deleting versions optimize is in the
middle of writing. The default hour of separation is far beyond a run's expected
duration; the Jobs' `activeDeadlineSeconds` (45m) is set below the gap so a hung
optimize is killed before cleanup starts.

## The addressing that works

Maintenance is **per graph**, and the flags are unforgiving. Verified against
omnigraph 0.8.1:

```sh
omnigraph optimize --cluster s3://ol-data-witan-<env> --graph council
```

- **`--cluster <storage-root-URI> --graph <id>` is the form to use.**
- Omitting `--graph` is a hard error, not an all-graphs default:
  `cluster '<uri>' has N graphs: [...]; pass --graph <id> to select one`.
  That is why the CronJobs loop over the graph list.
- Passing the *config directory* (`--cluster /etc/omnigraph/cluster`) fails with
  `has no applied state`. The cluster state ledger lives under the storage root
  (`__cluster/state.json`), not next to `cluster.yaml`.
- A bare positional URI (`omnigraph optimize s3://ol-data-witan-<env>`) addresses
  a *single graph store*, not the cluster root, and errors. It reads like the
  right command and is not.
- On an `s3://` store, `cleanup` needs **both** `--confirm` (to arm the
  destructive run) and `--yes` (to skip the confirmation a non-local scope
  demands). A pod has no TTY, so without `--yes` every run refuses having
  deleted nothing.

The graph list the sweeps cover is exactly what `cluster.yaml` declares —
`council`, `code-bridge`, and one `code-<repo>` per managed repo. Adding a repo
to `omnigraph:managed_repos` extends the sweep in the same deploy that creates
the graph.

## Why retention is an age, not a version count

`cleanup` accepts `--keep <N>` and `--older-than <duration>`. The CLI reports a
combined policy without stating whether it intersects or unions them — and the
two readings differ in safety, because a union would let `--keep` delete
versions younger than the age cutoff. The CronJob passes `--older-than` alone,
which is safe either way.

30d is sized against what actually needs the history: witan-code's per-writer
WIP branch views, which are per-session/per-git-branch and measured in days,
plus margin for time-travel reads. The cost is that a heavily-written graph
carries up to 30 days of dead versions — bounded storage. Query latency is
driven by fragment count, which the nightly optimize handles.

## Reading a failed run

```sh
kubectl -n omnigraph get cronjob
kubectl -n omnigraph get jobs -l app.kubernetes.io/name=omnigraph-optimize
kubectl -n omnigraph logs job/<job-name>
```

The sweep does **not** stop at the first failing graph. It logs
`!!! omnigraph <command> failed for <graph>`, continues, and exits non-zero at
the end with the full failing set:

```
!!! omnigraph optimize failed for: code-github-com-mitodl-some-repo
```

So a red Job means "at least one graph was not maintained", not "nothing was
maintained". The last line of a clean run is
`omnigraph <command>: all graphs completed`.

There is no retry (`backoffLimit: 0`). Both commands are idempotent so a retry
would be safe, but neither is urgent — the next scheduled run is the retry, and
an immediate re-attempt of a run that failed on a held lock fails the same way
while making the Job history harder to read.

### `graph 'X' is not applied in cluster ...`

The sweep's graph list and the cluster's applied state have diverged. Normally
impossible — both come from the same `build_cluster_graphs` call, and the
CronJobs depend on the converge Job — so this means the converge Job did not
run or did not succeed for that graph. Check it:

```sh
kubectl -n omnigraph get jobs -l app.kubernetes.io/name=omnigraph-cluster-apply
```

Re-running `pulumi up` re-runs convergence.

## `omnigraph repair` — manual only, deliberately

`repair` reconciles manifest/head drift. It is **not** scheduled, and should not
be: it is reactive, and its `--force` mode publishes drift that nothing has
verified. Run it when a graph fails to open or reports inconsistent state.

Preview first — without `--confirm` it only reports what it would do:

```sh
kubectl -n omnigraph run omnigraph-repair --rm -it --restart=Never \
  --image=<the image the omnigraph-server Deployment is running> \
  --overrides='{"spec":{"serviceAccountName":"omnigraph-server"}}' \
  --command -- omnigraph repair \
    --cluster s3://ol-data-witan-<env> --graph <graph-id> --as svc-witan-admin
```

Then re-run with `--confirm` to publish *verified* drift. `--force` (which also
publishes suspicious or unverifiable drift) requires operator review first —
read the preview output and understand each item before reaching for it.

Use the exact image the Deployment is running, not `:latest`. Storage is
strict-single-version — a binary reads exactly one storage-format version — so a
maintenance CLI at a different version than the server writing the store is the
mixed-fleet case upstream documents as unsupported. This is also why the
CronJobs pin the same digest the Deployment does.

## What this does not cover

- **A storage-format bump.** That cannot be done by restart or by any command
  here; it needs the offline export → rebuild → repoint procedure. Tracked
  separately (`tk-runbook-adr-addendum-omnigraph-storage-format-bu-a2032d`).
- **A quarantined graph.** By default the server logs a graph that fails to open
  and serves the rest, and `/healthz` stays 200 throughout — so a quarantined
  `council` is a silent brownout that no probe catches. `OMNIGRAPH_REQUIRE_ALL_GRAPHS`
  is deliberately left unset (see the rationale in `data_tier.py`); detecting it
  belongs with the service's monitoring.
