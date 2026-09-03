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
   objects is needlessly slow. Bucket versioning is enabled with no
   `NoncurrentVersionExpiration` rule, so the pre-rebuild Lance versions are
   *also* independently recoverable — but do not rely on that alone as the
   rollback plan.

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

## What this does not cover

- **A storage-format bump.** That cannot be done by restart or by any command
  here; it needs the offline export → rebuild → repoint procedure. Tracked
  separately (`tk-runbook-adr-addendum-omnigraph-storage-format-bu-a2032d`).
- **A quarantined graph.** By default the server logs a graph that fails to open
  and serves the rest, and `/healthz` stays 200 throughout — so a quarantined
  `council` is a silent brownout that no probe catches. `OMNIGRAPH_REQUIRE_ALL_GRAPHS`
  is deliberately left unset (see the rationale in `data_tier.py`); detecting it
  belongs with the service's monitoring.
