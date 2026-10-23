# omnigraph store maintenance runbook

What keeps the witan graph store from degrading, what runs on a schedule, and
what to do by hand when it drifts.

## Which model an environment is on

It depends on the storage format the environment serves
(`omnigraph:internal_schema_version`).

- Format 9 (omnigraph 0.11): two CronJobs sweep the store beside the running
  server. "What runs on its own" below describes them.
- Format 14 (omnigraph 0.12 and later): every writer of the storage root
  keeps an exclusive lock, so `optimize` and `cleanup` are refused with
  `state_lock_held` while the server is up. One CronJob stops the server for a
  nightly window instead. See "The nightly stop window (format 14)". On this
  format, do not run `optimize`, `cleanup` or `repair` by hand against a
  serving store. The statements below that compaction is safe beside a serving
  server apply to format 9 only.

## The nightly stop window (format 14)

`omnigraph-maintenance` runs at `20 3 * * *` UTC
(`omnigraph:maintenance_schedule`) as the `omnigraph-maintenance`
ServiceAccount. The data tier is down from the start of the run until it
finishes, for every witan user. The command is in the server image (agent-kit
`docker/omnigraph_maintenance.py`), and it:

1. skips the run if the Deployment carries `ol.mit.edu/migration-armed` or is
   at 0 replicas;
2. takes the `omnigraph-unlock` Lease, scales `omnigraph-server` to 0 and
   waits for its pods to exit;
3. force-unlocks the `serve` lock the server left;
4. runs `optimize` on every graph in `cluster.yaml`, then on the days in
   `omnigraph:cleanup_iso_weekdays` (default every day; Production and QA set
   `[]`, so their windows only optimize) also
   `cleanup --older-than <cleanup_older_than>`, force-unlocking after each;
5. scales the Deployment back to the replica count it found and releases the
   Lease.

No new command starts after 25 minutes, a single command is stopped after 10,
and the Job's deadline is 36 minutes, so the window is over before the CI
indexer's 04:00 run. Graphs it did not reach are named in the log and fail the
Job, and nothing is left locked. Every graph is optimized before any is
cleaned, and the cleanup pass starts at a different graph each night, so a
window that runs out delays a graph's cleanup to a later night. That does not
help a graph whose cleanup alone takes more than 10 minutes. Cleanup plans
every version present before it deletes any, so a run stopped by the timeout
deletes nothing, and that graph is stopped every night, never cleaned, and its
backlog grows. Clear it with [a longer run](#clearing-a-cleanup-backlog), and
see "Why retention is an age, not a version count" for why retention alone
does not keep it from coming back.

The council probe does not run between 03:00 and 03:59 on this format. Its
04:00 run is the check that the window ended, so a failed
`witan-council-probe` at 04:00 means: look at the last `omnigraph-maintenance`
Job first.

### When the window leaves the server down

The Job exits non-zero and its log says which case it is.
`WitanDataTierScaledToZero*` fires once the Deployment has declared 0 replicas
for 45 minutes, which is longer than a window that ended normally.

- `leaving omnigraph-server at 0 replicas (runbook: clear the lock, then scale
  up)`: the window could not remove its own `graph_operation` lock (it lost
  the Lease, or `cluster status` / `force-unlock` failed). A server started on
  that root refuses to boot.
- `omnigraph-server is still at 0 replicas (runbook: scale it back up)`: the
  root is unlocked and only the scale-up failed. Skip to step 3.
- No closing line at all: the pod was killed (OOM, lost node). Treat it as the
  first case.

To recover:

1. Confirm nothing is writing: no `omnigraph-maintenance` pod and no
   `omnigraph-server` pod.

   ```bash
   kubectl -n omnigraph get pods
   kubectl -n omnigraph get lease omnigraph-unlock -o yaml
   ```

   A Lease whose `renewTime` is older than 65 seconds, or whose
   `holderIdentity` is empty, is not held.

2. Read the lock and remove exactly that id, from a pod of the server image
   with the server's S3 access:

   ```bash
   omnigraph cluster status --cluster <storage root> --json
   omnigraph cluster force-unlock --cluster <storage root> <lock_id>
   ```

   `<storage root>` is the stack's `storage_uri` output
   (`s3://ol-data-witan-<env>/<storage_prefix>`).

   `force-unlock` has no conditional delete. Running it while a writer is
   alive removes that writer's lock under it.

3. Scale the server back up. Its entrypoint takes the Lease and boots.

   ```bash
   kubectl -n omnigraph scale deployment omnigraph-server --replicas=1
   ```

The next night's window skips while the Deployment is at 0, so this does not
heal on its own.

### Clearing a cleanup backlog

A graph whose cleanup log says `` `omnigraph cleanup ... --graph <id> ...` did
not finish in 600s `` night after night has more versions than one command can
plan. Size it before you schedule the run. A cleanup costs 0.16 to 0.22s per
object under the graph's `_versions/` directories, 0.19s on average
(Production, 2026-10-20: 4,026s over 21,021 versions on six graphs). The
count includes the commit manifests under `__manifest/`, which cleanup reads
but never removes:

```bash
aws s3 ls --recursive s3://ol-data-witan-<env>/<storage_prefix>/graphs/<id>.omni/ \
  | grep -c '/_versions/'
```

The run is the nightly window with a longer budget, so the data tier is down
for every witan user until it ends. Announce it, and keep it clear of the
03:20 window and the CI indexer's runs. Create a one-off Job from the CronJob
with the window's limits raised. `activeDeadlineSeconds` has to stay above the
window plus one command's timeout, or the deadline fires before the command
closes its own window:

```bash
kubectl -n omnigraph create job omnigraph-backlog-cleanup \
  --from=cronjob/omnigraph-maintenance --dry-run=client -o json \
  | jq '.spec.activeDeadlineSeconds = 7500
        | .spec.template.spec.containers[0].env |= map(
            if .name == "OMNIGRAPH_WINDOW_SECONDS" then .value = "5400"
            elif .name == "OMNIGRAPH_COMMAND_TIMEOUT_SECONDS" then .value = "1800"
            elif .name == "OMNIGRAPH_CLEANUP_ISO_WEEKDAY" then .value = "1,2,3,4,5,6,7"
            elif .name == "OMNIGRAPH_CLEANUP_OLDER_THAN" then .value = "2d"
            else . end)' \
  | kubectl apply -f -
kubectl -n omnigraph logs -f job/omnigraph-backlog-cleanup
```

Size the command timeout at 0.22s per version plus headroom: 1800s covers
about 8,000 versions on one graph, and the window has to hold the sum over
every graph (all of Production was about 25,000 on 2026-10-21, about 90
minutes at 0.22s). The example's 5400s window is too short for all of
Production, so compute the window from the count summed over every graph, plus
about a minute for the optimize pass, and raise the timeout, the window and
`activeDeadlineSeconds` together: a deadline that fires mid-plan stops the run
with nothing deleted. An image that predates
comma lists in `OMNIGRAPH_CLEANUP_ISO_WEEKDAY` refuses `1,2,3,4,5,6,7` before
it stops anything; give it today's ISO weekday (`date -u +%u`) instead. The
Job ends with `maintenance window complete: all graphs maintained` and the
server back at its replica count. Anything else is the
section above. Delete the Job afterwards
(`kubectl -n omnigraph delete job omnigraph-backlog-cleanup`), because a
failed Job keeps `WorkloadJobFailed*` firing for as long as it exists.

## What runs on its own

Two CronJobs in the `omnigraph` namespace on format 9, both defined in
`src/ol_infrastructure/applications/omnigraph/maintenance.py`:

| CronJob | Default schedule | What it does | Destructive |
|---|---|---|---|
| `omnigraph-optimize` | `20 3 * * *` (nightly) | Compacts small Lance fragments in every table of every graph | No |
| `omnigraph-cleanup` | `20 4 * * 0` (Sundays) | Removes Lance versions older than `cleanup_older_than` (2d) | **Yes** |

Both run the `omnigraph` CLI baked into the `omnigraph-server` image, as the
`omnigraph-server` IRSA service account, **against the S3 store directly** —
never through the server. That is not a shortcut: `optimize`, `cleanup` and
`repair` are direct-storage commands and reject `--server` outright.

Running compaction while the server is serving is safe. omnigraph's publisher
uses compare-and-swap, so a maintenance write that loses a race refreshes and
retries rather than corrupting anything.

Per-environment overrides (`pulumi config set omnigraph:<key>`):

- `optimize_schedule`, `cleanup_schedule` — cron expressions, in UTC.
- `cleanup_older_than` — a Go-style duration (`2d`, `72h`).

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

`cleanup` accepts `--keep <N>` and `--older-than <duration>`, and a graph
commit either one retains survives. `--keep` counts commits per live branch,
and the code graphs carry a branch per writer, so the window passes
`--older-than` alone.

The default, 2d, run every night, was chosen to fit the stop window in CI and
QA, where commits are few. It no longer fits there. On format 14
cleanup runs with the server down, and before it deletes anything it opens
every version present, retained or about to be removed, at 0.16 to 0.22s each.
Retention bounds only the table versions. The commit manifests
(`__manifest/_versions/`, and `__manifest/tree/*/_versions/` for each writer
branch) are planned too, and 0.13's cleanup never removes them: every delete
marker under Production's council and ol-data-platform is a table version.
They grow by one per commit. In Production on 2026-10-21 they were 11,800 of
25,372 versions, about 2,240s of plan against a 1,500s window, and grew by
about 1,500 a day, so no retention fits Production's window and its
`omnigraph:cleanup_iso_weekdays` is `[]`. QA's is `[]` for the same reason on
one graph: its code-bridge cleanup took 413s for 2,559 versions on 2026-10-21
(0.16s each), the first night after a backlog run. With about 1,200 table
versions retained at 2d its 2026-10-22 plan is about 3,150 versions (510s),
and the indexer adds about 255 manifests a day, about 40s a night, so it would
pass the 600s timeout around 2026-10-25. CI still has the nightly default,
and its code-bridge had 4,744 versions on 2026-10-21, over the timeout
already, so CI's window fails every night (and `WorkloadJobFailed*` fires)
until CI gets a backlog run or `[]` too. Upstream: ModernRelay/omnigraph#1095.

witan's own code needs no older versions. Every live branch head is kept
regardless of age, which covers witan-code's per-writer WIP views, and witan
makes no time-travel reads. What 2d gives up is the operator's undelete: a
hard-deleted witan row is readable through `omnigraph query --snapshot` only
for as long as the commit before the delete is retained. Past that, and for a
bad cleanup, the restore is the bucket's noncurrent object versions (30 days)
or an `aws s3 sync` backup.
Query latency is driven by fragment count, which the nightly optimize handles.

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

## `omnigraph rebuild-full-text-indexes` — an analyzer-generation cutover

A third category, and the one that reads most like the other two while behaving
least like them. `optimize` and `cleanup` are safe to run against a serving
fleet; this is not. Run it when an upgrade changes the **full-text analyzer** —
as omnigraph 0.10.0 did, upgrading Lance to 11 (upstream #581).

First run in production on 2026-09-01; the notes below are measured from it,
against omnigraph 0.10.0.

### Why nothing warns you

The format version does not move: `omnigraph version` still reports
`internal-schema 6` on both sides of the upgrade, so agent-kit's pin gate
(`bin/check_omnigraph_format.py`, against `_OMNIGRAPH_INTERNAL_SCHEMA`) stays
**green — correctly**, because the on-disk format really is unchanged. Analyzer
generation sits outside what that gate covers, and outside what the
storage-format runbook's "is this actually a format bump?" check answers. There
is no automated signal; the trigger is reading the upstream release notes.

0.10.0 fails **closed**, which is the one mercy here: an index whose analyzer
generation cannot be proven compatible raises `FullTextIndexRebuildRequired`
(HTTP 409, detail key `full_text_index_rebuild_required`). Ordinary reads keep
working and no partial indexed result is returned, so the blast radius is
"search is refused", not "search quietly lies". Raw Lance under-returns silently
on a generation mismatch; the guard exists to turn that into an error.

### It is not just `council`, and not just `main`

`@index` on a String property produces a **full-text** index, so grepping
`read.gq` for `search()`/`bm25()` call sites under-counts the work. Measured
index counts per branch:

| Graph | Indexes | Node types |
|---|---|---|
| `council` | 34 | Memory, Task, Topic, WorkflowProject, WorkflowSession, WorkflowTrace, CodeBranch |
| `code-bridge` | 16 | InterfaceBinding, RepoSymbol, PackageMap |
| each `code-<repo>` | 9 | CodeFile, Symbol |

The rebuild is **per branch** (`--branch`, default `main`); other branches and
historical snapshots are untouched. So enumerate rather than assuming main-only
— witan-code's per-actor WIP views (agent-kit ADR-0006) count, and the view
reaper means the set changes week to week. Production in 2026-09 was 16 graphs
but **18** (graph, branch) pairs:

```sh
for g in $(omnigraph graphs list --server http://127.0.0.1:8080 | cut -f1); do
  for b in $(omnigraph branch list --server http://127.0.0.1:8080 --graph "$g"); do
    printf '%s\t%s\n' "$g" "$b"
  done
done
```

That needs a bearer token; read `svc-witan-admin` out of the server's own
`/etc/omnigraph/actor-tokens/tokens.json` and export it as
`OMNIGRAPH_BEARER_TOKEN` (no subcommand takes a token flag). Run it before
scaling the server down, since it asks the server.

The per-repo `code-<repo>` graphs are re-derivable, so a full reindex is an
alternative remedy for those. `council` is **not** — it must be rebuilt in place.

### The ordering trap

**Rebuild with the NEW binary, while the old fleet is stopped, BEFORE rolling
the image.** The rebuild writes an index using whichever binary runs it, so
rebuilding with the currently-deployed (old) binary produces an index the new
server then refuses — the maintenance window is spent and the outage still
arrives.

Check what is actually deployed rather than trusting the version string. Both
the pre-#581 `edge` pin and the released v0.10.0 report `omnigraph 0.10.0`, and
only the latter is Lance 11. The cheap discriminator is the subcommand itself,
which does not exist on the older build:

```sh
kubectl -n omnigraph exec deployment/omnigraph-server -- \
  omnigraph rebuild-full-text-indexes --help   # `unrecognized subcommand` = pre-#581
```

### Procedure

1. **Stop everything that writes the graph.** That is four CronJobs across two
   namespaces, not just the two in this runbook's table:

   ```sh
   kubectl -n omnigraph patch cronjob omnigraph-optimize omnigraph-cleanup \
     --type=merge -p '{"spec":{"suspend":true}}'
   kubectl -n witan patch cronjob witan-ci-indexer witan-view-reaper \
     --type=merge -p '{"spec":{"suspend":true}}'
   ```

   Scaling the Deployments to zero does **not** stop the two omnigraph sweeps —
   they write S3 directly, behind the server's back. That is the same property
   the storage-format runbook suspends them for.

2. **Scale both tiers down** and wait for the pods to actually go:

   ```sh
   kubectl -n witan scale deployment/witan-server --replicas=0
   kubectl -n omnigraph scale deployment/omnigraph-server --replicas=0
   kubectl -n witan wait --for=delete pod \
     -l app.kubernetes.io/name=witan-server --timeout=240s
   kubectl -n omnigraph wait --for=delete pod \
     -l app.kubernetes.io/name=omnigraph-server --timeout=240s
   ```

3. **Back up the graph roots and `__cluster` first.** Parallelise one
   `aws s3 sync` per graph and write a completion marker; a serial sync of ~90k
   objects is needlessly slow. Bucket versioning is enabled and noncurrent
   versions expire after 30 days, so the pre-rebuild Lance versions are *also*
   recoverable by version id within that window, but do not rely on that alone
   as the rollback plan.

4. **Rebuild, per graph and per branch, on the new image:**

   ```sh
   kubectl -n omnigraph run omnigraph-fts-rebuild --rm -it --restart=Never \
     --image=<the NEW image, the one about to be deployed> \
     --overrides='{"spec":{"serviceAccountName":"omnigraph-server"}}' \
     --command -- omnigraph rebuild-full-text-indexes \
       --cluster s3://ol-data-witan-<env>/<storage-prefix> \
       --graph <graph-id> --branch <branch> \
       --as svc-witan-admin --json
   ```

   Note the **storage prefix**: the cluster root is the stack's `storage_uri`
   output (`s3://ol-data-witan-production/fmt6` today), which is what the
   CronJobs get as `OMNIGRAPH_STORAGE_ROOT`. That is the form verified here.

   Unlike `optimize`/`cleanup`, this command **accepts `--as`** — it is
   actor-bound, and the actor attributes the write. `--json` gives you a
   `graph_commit_id` and a `rebuilt_indexes` list per target, which is the
   evidence worth keeping. Prefer one pod running a loop over all targets to 18
   pod startups.

5. **Then roll the images** (`pulumi-omnigraph` before `pulumi-witan`). The
   Pulumi deploys reconcile the suspend flags and replica counts back to
   declared state, so steps 1–2 need no manual undo — but verify rather than
   assume: `witan-break-glass` must be `true`, every other CronJob `false`.

### Verifying it worked

The thing Lance 11 changes is English **stemming**, so test that specifically:
query a stem and its morphological variant and assert the returned **slugs** are
identical (`cluster`/`clusters`, `write`/`writes`, `migration`/`migrations`).

**Do not compare hit counts.** They saturate at the CLI's 20-row display limit
and pass even on a broken index — the check has to compare identity, not volume.

Also confirm zero `requires rebuild` / `full_text_index_rebuild_required` lines
in `omnigraph-server` logs after restart, and exercise the code side
(`code_search_symbol`, and `witan-code deps` for `code-bridge`) rather than only
the memory side.

### Cost, measured

Production, 16 graphs / 18 branches / 3.97 GB (90,634 objects): **~24 minutes of
write outage** end to end (01:43–02:07 UTC), of which the rebuild itself was
only ~2 minutes. The backup and the two image rolls dominate, and the witan roll
in particular waits on its pre-deploy migration Job — measured at 8m on QA and
11m on production, so budget the larger figure.

### Which principal, and why not break-glass

This is a **direct-storage** command gated by **AWS IAM** on the bucket, like
`optimize`/`cleanup`/`repair` — run it on the `omnigraph-server` ServiceAccount,
which carries the bucket's IRSA grant. Cedar is not what authorizes it, so the
witan break-glass pod (agent-kit ADR-0005 path b) is the wrong path despite
`svc-witan-admin` appearing in the command line.

## Rebuilding a graph whose schema change cannot be migrated

Some schema changes are refused by `schema plan` (`supported: false`, e.g.
removing a column), and then the plan gate fails every deploy of the image that
carries them. omnigraph 0.13's `cluster apply` has no replace operation, and its
plan is keyed by graph id, so a single `cluster.yaml` cannot both delete and
create a graph. The rebuild is two deploys, both stack config:

1. Add the graph id to both `omnigraph:withheld_graphs` and
   `omnigraph:graph_removal_allowlist` in `Pulumi.<env>.yaml` (preview refuses
   a withheld graph that is not allow-listed). The deploy drops
   the graph and its Cedar bundle from `cluster.yaml` (a bundle naming an
   undeclared graph fails the plan with `dangling_graph_reference`), and the
   server's boot apply deletes every object under
   `<storage_root>/graphs/<id>.omni/`, `_versions/` and `__manifest/` included.
   The bucket keeps the deleted objects as noncurrent versions for 30 days.
2. After that deploy has passed, remove both keys. The next boot apply creates
   the graph empty from the image's schema. Do not merge this before step 1's
   deploy has run: Concourse can skip an intermediate commit, and a deploy of
   step 2 alone plans the refused migration again.
3. Refill it. `council` is refused by `withheld_graphs`, since nothing
   re-derives it. A rebuilt `code-<repo>` graph refills on the CI indexer's
   next scheduled run: it skips a file only when the graph holds that file's
   content hash, and the new graph holds none. `code-bridge` needs a forced
   reindex of every managed repo, because the per-repo graphs still hold their
   hashes, so an incremental run skips unchanged files and writes no bridge
   bindings for them:

```bash
script="sed 's/witan code index [.]/witan code reindex ./' /usr/local/bin/witan-ci-index > /scratch/witan-ci-reindex.sh && grep -q 'witan code reindex [.]' /scratch/witan-ci-reindex.sh && exec sh /scratch/witan-ci-reindex.sh"
kubectl -n witan create job witan-ci-reindex-$(date -u +%Y%m%d%H%M) \
  --from=cronjob/witan-ci-indexer --dry-run=client -o json \
  | jq --arg script "$script" '.spec.template.spec.containers[0].command = ["sh", "-c", $script]' \
  | kubectl -n witan create -f -
```

Start it right after a scheduled `witan-ci-indexer` run (every 4 hours, on the
hour) has finished. The CronJob's `Forbid` does not see a Job created by hand,
and two sweeps writing the same default views at once is the multi-writer case
the indexer avoids. The Job inherits the 3-hour deadline, so one started within
an hour of the scheduled run ends before the next. It exits non-zero if any
repo failed and names them on its last line
(`witan-ci-index: indexed N, failed M`). Delete it when it is done, since a
failed Job keeps `WorkloadJobFailed*` firing for as long as it exists.

### The delete outlasts the startup probe

Step 1's delete runs inside the boot apply, before the server listens, so it
runs under the startup probe. The first probe is at 20s and the 24th failure
at 135s (20 + 23 × 5), when kubelet sends SIGTERM. SIGTERM does not stop
the apply, which keeps deleting through the 125s termination grace until
SIGKILL, so a delete gets about 4 minutes. In-cluster it deletes about 1,000
objects a minute. On 2026-10-22 CI's polled object count fell from 15,438 at
16:17:25Z to 11,848 at 16:21:05Z, then stopped at 11,356 left. Every later
boot then refuses with `an earlier cluster apply was interrupted and its
deployment is still outstanding`, and the server stays down until the
deployment is reconciled by hand. Count the objects first
(`aws s3 ls --recursive <storage_root>/graphs/<id>.omni/ | wc -l`). Anything
over about 3,500 will be killed at the default budget.

For a larger graph, set `omnigraph:boot_budget_seconds` in step 1's change,
sized from the count at 1,000 objects a minute with room to spare (e.g. 3600
for 25,000 objects), and remove it in step 2's. It raises the startup probe's
failure threshold, and the Deployment's progress deadline and Pulumi's wait
for the rollout with it, so the deploy waits out the delete instead of failing
at Pulumi's 10-minute default. The server is down for the whole delete.

If a delete is killed anyway, reconcile it by hand:

```bash
kubectl -n omnigraph scale deploy/omnigraph-server --replicas=0
eval "$(aws configure export-credentials --format env)" && export AWS_REGION=us-east-1
root=s3://<bucket>/<storage_prefix>
omnigraph cluster status --cluster "$root" --json   # outstanding_id, lock_id
aws s3 cp "$root/__cluster/lock.json" -              # confirm it is the killed pod's `deployment` lock
omnigraph cluster force-unlock --cluster "$root" <lock_id>
omnigraph cluster apply --cluster "$root" --deployment-id <outstanding_id> \
  --writers-stopped --as svc-witan-admin --json      # finishes the delete; ends "converged": true
omnigraph cluster force-unlock --cluster "$root" <new lock_id from lock.json>
kubectl -n omnigraph scale deploy/omnigraph-server --replicas=1
```

Scale to 0 first: `--writers-stopped` attests that nothing else writes the
root, and the crash-looping pod would. Only force-unlock a lock once its
owner has exited and the graph's object count has stopped moving. The
root-addressed apply leaves its own lock with operation `deployment_reconcile`
after it succeeds. The entrypoint takes over only `serve` and `deployment`
locks, so release that one by hand, or the next boot refuses it. From a
laptop the reconcile deleted CI's remaining 11,356 objects in about 35
minutes. Step 2's create is quick and stays inside the probe.

Keep both deploys clear of the 03:20 window (and its end by about 04:00). A
`pulumi up` during it scales the server back to 1, and the new pod waits on the
held Lease until the window closes, so the Deployment update can time out.
Between the two deploys every bridge write fails with `ClusterGraphMissing`
(`witan.code.index.bridge_failed`, logged at error, not fatal to the index), and
the reaper's `code-bridge` sweep fails if it runs then (04:50).

## What this does not cover

- **A storage-format bump.** That cannot be done by restart or by any command
  here; it needs the offline export → rebuild → repoint procedure. Tracked
  separately (`tk-runbook-adr-addendum-omnigraph-storage-format-bu-a2032d`).
- **A blocked graph.** By default the server logs a graph that fails to open
  and serves the rest, and `/healthz` and `/readyz` stay 200 throughout
  (`/readyz` reports `"status":"degraded"` and a `blocked_graph_count`, not
  which graph) — so a blocked `council` is a silent brownout that no probe
  catches. `OMNIGRAPH_REQUIRE_ALL_GRAPHS`
  is deliberately left unset (see the rationale in `data_tier.py`); detecting it
  belongs with the service's monitoring.
