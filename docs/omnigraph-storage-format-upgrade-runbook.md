# omnigraph storage-format upgrade runbook

How to roll out an `omnigraph-server` image whose binary bumps the **storage
format**, why the ordinary deploy cannot do it, and how to tell the two cases
apart before you start.

Every command here was run against the live CI deployment on 2026-08-05
(`operations-ci`, omnigraph 0.8.1, `internal-schema 4`), including a full
rehearsal of the rebuild — `cluster import` → `cluster apply` → `load` →
verification — on a scratch prefix with synthetic rows, which round-tripped
exactly. The one thing not exercised is the version gate itself: no
format-bumping image exists yet, so the drill ran the same binary on both
sides.

## When this applies

omnigraph storage is strict-single-version: a binary reads exactly one
internal-schema (storage-format) version, there is no in-place migration, and
the version gate is enforced in both directions — including read-only opens. A
mixed fleet where an old binary still writes a graph a newer binary has stamped
is unsupported; there is no mixed-version window.

The data tier is already built for the *same*-format case. `applications/omnigraph`
runs a single replica with `strategy=Recreate` (`data_tier.py`), so the old pod
is gone before the new one starts and the two binaries never touch S3 at once.
That is the whole of what Recreate buys, and it is enough for every normal image
bump.

It is **not** enough when the new binary changes the format. Recreate still
starts the new pod against a store the new binary refuses to open, and the
rollout fails on the version gate rather than corrupting anything. The graph is
intact; it is simply unreadable by the image you just shipped, and stays that
way until you either roll back or run the offline rebuild below.

> This is the "offline export/rebuild" path referenced from `data_tier.py`'s
> `strategy=Recreate` comment and from
> [ADR-0009](adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md).

## First: is this actually a format bump?

Most image bumps are not, and running this procedure on one buys an outage for
nothing. There are two numbers, and only one of them is the gate.

**The binary's format version** — `omnigraph version`, second line:

```console
$ kubectl -n omnigraph exec deploy/omnigraph-server -- omnigraph version
omnigraph 0.8.1
internal-schema 4
```

**The store's format version** — `omnigraph snapshot`, third line:

```console
$ kubectl -n omnigraph exec deploy/omnigraph-server -- \
    omnigraph snapshot --store s3://ol-data-witan-ci/graphs/council.omni
branch: main
manifest_version: 4
internal_schema_version: 4
edge:Addresses v1 branch=main rows=0
…
```

> **`manifest_version` is not the format version.** It is a per-store manifest
> revision counter and legitimately differs between graphs in the same cluster —
> on CI, `council` reads `manifest_version: 4` and
> `code-github-com-mitodl-agent-kit` reads `3`, while both are
> `internal_schema_version: 4`. Compare `internal-schema` (binary) against
> `internal_schema_version` (store). Nothing else.

Ask the candidate image directly rather than reading release notes — both images
carry the `omnigraph` CLI alongside the server:

```shell
kubectl -n omnigraph run omnigraph-version-probe --rm -it --restart=Never \
  --image=<new-image-ref> --command -- omnigraph version
```

Same number as the deployed stores → ordinary deploy, stop reading. Different
number → continue.

## Before you start

- **Pause the `pulumi-omnigraph` Concourse pipeline.** The chain is
  `build-omnigraph-server-image` → CI → QA → Production, each stage triggering
  on the previous stage having deployed *that same image*
  (`src/ol_concourse/pipelines/infrastructure/omnigraph/pipeline.py`). Unpaused,
  it keeps promoting a format-bumping image into the next environment while you
  are still migrating this one. Pause it before the build lands, or as soon as
  you see the failed CI deploy.
- **CI, then QA, then Production**, each with a soak. This rebuilds every graph;
  do it once somewhere cheap first.
- **Size the outage.** The data tier is down for the whole procedure — this is
  not a rolling change. Duration scales with total graph size.

  As of 2026-08-05 every environment's `council` is empty (0 rows in every
  table) and the code graphs are effectively unpopulated, so a migration today
  is minutes. Re-measure once real data lands rather than trusting that number.
- **Write down the old image digest.** It is the rollback.

## What gets rebuilt

Every graph in the cluster, not just `council`. `build_cluster_graphs()`
declares:

| Graph id | Schema | Source |
| --- | --- | --- |
| `council` | `schema.pg` | fixed — the Layer-1 memory/task/workflow graph |
| `code-bridge` | `bridge-schema.pg` | fixed — the Layer-2.5 cross-repo bridge |
| `code-<repo>` | `code-schema.pg` | one per entry in `omnigraph:managed_repos` |

CI currently declares 16 graphs. Enumerate from the running config, not from
`managed_repos` — the ids are derived, and re-deriving them by hand is how you
drop one. Produce a plain one-id-per-line list, because every loop below reads
it:

```shell
kubectl -n omnigraph get configmap omnigraph-cluster-config \
  -o jsonpath='{.data.cluster\.yaml}' | yq -r '.graphs | keys | .[]' \
  > /tmp/graph-ids.txt
wc -l /tmp/graph-ids.txt
```

Keep that file — every loop in the procedure reads it. It lives on your
workstation, and steps 2 and 4 each copy it into their workspace pod, which has
no ConfigMap mount of its own.

Then cross-check against what is actually in the bucket, which is not
necessarily the same set:

```shell
aws s3 ls s3://ol-data-witan-<env>/graphs/
```

CI has a `main.omni/` in the bucket that no longer appears in cluster.yaml — a
leftover from before the `council` rename. Undeclared stores are not served and
not migrated by `cluster apply`; decide deliberately whether each one is dead
(leave it, or clean it up separately) rather than discovering it mid-migration.

## The shape of the migration

The old store is never modified and never moved. Lance embeds absolute paths, so
a store cannot be relocated with `mv`/`cp` — only rebuilt via `export` →
`init`/`load`. That constraint drives the design:

**Rebuild under a new storage root, keeping every graph id identical.** Clients
address graphs by id (`WITAN_MEMORY_GRAPH=council`; witan-code's `graph_id()`
for `code-<repo>`), never by storage path, so a new root is invisible to them.
The old root stays byte-for-byte intact, making rollback a config change rather
than a restore.

The root can be a **prefix inside the existing bucket** — `cluster validate`
accepts `storage: s3://ol-data-witan-ci/_fmt-drill`, verified 2026-08-05. So no
new bucket, no new IRSA policy (the existing grant is `<bucket-arn>/*`), and no
change to backups. Use `s3://ol-data-witan-<env>/fmt<N>` for new format `<N>`.

The alternative — rebuilding in place at the same paths — would require deleting
each old store first, leaving S3 object versioning as the only rollback. Do not
do that. Reassembling a Lance store from thousands of object versions is not a
recovery path anyone should be attempting under time pressure.

Repointing is a config change: **`omnigraph:storage_prefix`**. Unset (the
steady state) puts the graphs at the bucket root; set to `fmt5` they live at
`s3://ol-data-witan-<env>/fmt5`. Step 5 sets it, rollback removes it, and no
code is edited during the outage.

It is a *prefix inside the managed bucket*, not a free-form URI, on purpose:
the bucket, its IAM policy and the IRSA grant are all keyed to the derived
name, so a full URI could aim the cluster at storage nothing has granted
access to — and that failure would land mid-migration. Keeping it inside the
bucket also means backups and versioning cover the new root for free. The
value is validated at `pulumi preview` time (single `[A-Za-z0-9._-]` segment,
no wrapping slashes), which is the only place it *can* be caught — see the
`cluster validate` note in step 4.

## Addressing: `--store`, not `--cluster`

The one thing most likely to waste your time. `--cluster <root> --graph <id>` is
for **maintenance** commands only (`optimize`, `repair`, `cleanup`, `cluster *`).
Data commands reject it:

```console
$ omnigraph snapshot --cluster s3://ol-data-witan-ci --graph council
Error: `snapshot` is a data command; --cluster addresses a cluster-scoped
command and does not apply.
```

`export`, `load`, `init`, and `snapshot` address a cluster graph by its store
URI, which is `<root>/graphs/<graph-id>.omni`:

```shell
omnigraph snapshot --store s3://ol-data-witan-ci/graphs/council.omni
```

The cluster's own state ledger lives at `<root>/__cluster/state.json`.

## The automated path (preferred)

Steps 2, 3, 4 and 6 below — baseline, export, rebuild, verify — run as a single
in-cluster Job. Use it. The manual procedure that follows is kept as the
reference for what the Job does, and as the fallback when something in the
middle needs picking apart by hand.

```shell
cd src/ol_infrastructure/applications/omnigraph

# The image currently deployed — its binary is the only one that can still read
# the old root. `kubectl -n omnigraph get deploy/omnigraph-server \
#   -o jsonpath='{.spec.template.spec.containers[0].image}'`
pulumi config set omnigraph:migrate_from_image <OLD-image-ref> --stack <CI|QA|Production>
# Where the rebuild lands. Digits = the NEW internal-schema number.
pulumi config set omnigraph:migrate_to_prefix fmt6 --stack <CI|QA|Production>

pulumi up --stack <CI|QA|Production>     # creates the Job; suspends both sweeps
kubectl -n omnigraph logs -f job/omnigraph-migrate-fmt6
```

**Two config knobs, and the distinction is the point.**
`migrate_to_prefix` is where the rebuild WRITES. `storage_prefix` is what the
cluster SERVES — setting it *is* the cutover, and it takes effect immediately
(`pulumi preview` shows `storage_uri` moving, the Deployment's config hash
rolling and both CronJobs following). Arming the migration on `storage_prefix`
would point the running server at an empty root before the rebuild had written
a row. The program refuses if both name the same prefix.

The Job runs one pod carrying **both** binaries: an initContainer on the old
image copies its `omnigraph` into a shared volume, and the main container runs
the new image. So every byte goes S3 → pod → S3, and the `kubectl exec`
transfer in steps 2–4 — the slowest part of the manual path, and the only step
that fails silently — does not happen at all.

It also **suspends `optimize` and `cleanup`** for the duration, because both
write directly to the store and scaling the Deployment to zero does not stop
them. Clearing `migrate_from_image` resumes them in the same `pulumi up`.

The Job stops after verification. It does **not** cut over: that is
`pulumi config set omnigraph:storage_prefix`, which needs Pulumi credentials a
workload has no business holding, and writing the ConfigMap instead would make
it a second writer of a path Pulumi owns. On success it prints the exact
cutover command and writes `/tmp/migration-verdict.json` (per-graph, per-table
before/after counts) for anything that wants to gate on it.

Then continue at **step 5** below, and finish with step 7.

If the Job fails, it fails clean: the old root is untouched, the cluster still
serves it, and the exports are still on the pod. Read the logs before deleting
it — `ttlSecondsAfterFinished` keeps it for a week precisely so the evidence
outlives the run.

## Procedure (manual)

Set these first, **on your workstation** — steps 5, 6 and 7 use them there:

```shell
ENV=ci                                    # ci | qa | production
OLD_ROOT="s3://ol-data-witan-$ENV"
NEW_ROOT="$OLD_ROOT/fmt5"                 # digits = the NEW internal-schema number
```

`fmt5` is an example. **Substitute the real number** — `<` and `>` are legal in
S3 object keys, so a literal `fmt<N>` becomes a real prefix and the migration
silently rebuilds into the wrong place. The pod-shell blocks below re-set these
and guard against exactly that; the same guard is worth running here:

```shell
case "$NEW_ROOT" in
  *'<'*|*'>'*) echo "NEW_ROOT still has a placeholder — stop"; exit 1 ;;
esac
printf '%s\n' "$NEW_ROOT" | grep -qE '^s3://[a-z0-9.-]+/fmt[0-9]+$' \
  || { echo "NEW_ROOT malformed: $NEW_ROOT — stop"; exit 1; }
```

Each pod shell you open below needs the same three set again — a `kubectl exec`
shell inherits nothing from your workstation, and the steps that open one say so
where it matters.

All `omnigraph` work runs **in-cluster**, in a one-off pod on the
`omnigraph-server` ServiceAccount, which carries the bucket's IRSA grant — no
human AWS credentials, and it is the `svc-witan-admin` break-glass identity
(agent-kit ADR-0005 path (b)) rather than a side channel. There is no
`omnigraph` binary on your workstation, and after step 1 there is no running
server pod to `exec` into either, so each step below says which of the two
workspace pods it runs in. Commands starting with `kubectl` are the ones you
run locally.

> **The image ships only `omnigraph`, `omnigraph-server`, and its entrypoint.**
> No `aws`, no `curl`, no `python3`. Anything moving data in or out of a
> workspace pod goes through `kubectl exec`, not an S3 round trip — which is why
> steps 3 and 4 hand the exports across rather than staging them in the bucket.
> `aws` commands in this runbook run on your **workstation**.

Two pods, because the migration spans two binaries: `omnigraph-migrate-old`
(step 2, baseline + export) and `omnigraph-migrate-new` (step 4, rebuild).
Both are cleaned up in step 7.

### 1. Take the graph out of the serving set

```shell
kubectl -n omnigraph scale deploy/omnigraph-server --replicas=0
kubectl -n omnigraph wait --for=delete pod \
  -l app.kubernetes.io/name=omnigraph-server --timeout=120s
```

Wait on the **pod being gone**, not on `rollout status`. The deployment
reconciles to zero long before the old server finishes terminating —
`omnigraph-server` uses the full SIGTERM grace period — and until that process
exits it is still a writer against the old root. `rollout status` reports the
Deployment converged and would let you proceed with the server still up.

Nothing may write to the old root from here until the migration completes or
rolls back. `optimize`/`cleanup` write directly to the store, bypassing the
server entirely, so scaling the Deployment to zero does **not** stop them.

The automated path suspends both when `migrate_from_image` is set. If you are
running the manual procedure, suspend them yourself — and note this is a real
exposure, not a formality: `optimize` rewrites Lance fragments, and a tick
landing between the step-3 export and the step-6 verification is a write to a
root this procedure has declared frozen.

```shell
kubectl -n omnigraph patch cronjob/omnigraph-optimize -p '{"spec":{"suspend":true}}'
kubectl -n omnigraph patch cronjob/omnigraph-cleanup  -p '{"spec":{"suspend":true}}'
kubectl -n omnigraph get jobs            # and confirm none is already mid-run
kubectl -n omnigraph get pods            # expect no omnigraph-server pod at all
```

> A hand-patched CronJob is reverted by the next `pulumi up` for anything at
> all, and a Production migration can span days. Prefer the config-driven
> suspension even if you are otherwise working through this manually — set
> `omnigraph:migrate_from_image` and the sweeps stay down until you clear it.

### 2. Start the OLD-image workspace pod

Step 1 scaled the Deployment to zero, so `kubectl exec deploy/omnigraph-server`
is gone with it and there is no `omnigraph` binary on your workstation. Steps 2
and 3 both need one, so start it here — a pod on the **currently-deployed**
image, which is the old binary that can still read the old root:

```shell
kubectl -n omnigraph run omnigraph-migrate-old --restart=Never \
  --image=<OLD-image-ref> \
  --overrides='{"apiVersion":"v1","spec":{"serviceAccountName":"omnigraph-server"}}' \
  --env=AWS_REGION=us-east-1 --command -- sleep 86400
kubectl -n omnigraph wait --for=condition=Ready pod/omnigraph-migrate-old --timeout=120s
kubectl -n omnigraph exec -i omnigraph-migrate-old -- sh -c 'cat > /tmp/graph-ids.txt' \
  < /tmp/graph-ids.txt
kubectl -n omnigraph exec -it omnigraph-migrate-old -- sh
```

Everything in steps 2 and 3 runs **inside this pod** unless it starts with
`kubectl`. Re-set the roots here — the pod shell inherits nothing — and
sanity-check the binary and its addressing before relying on either:

```shell
ENV=ci                                    # ci | qa | production
OLD_ROOT="s3://ol-data-witan-$ENV"
NEW_ROOT="$OLD_ROOT/fmt5"                 # real digits — see the guard in step 4
omnigraph version
omnigraph snapshot --store "$OLD_ROOT/graphs/council.omni" | head -3
```

Then record the baseline — per-table row counts for every graph. This is what
step 6 checks against, and it is the only thing that catches a load which
silently dropped a table:

```shell
for g in $(cat /tmp/graph-ids.txt); do
  echo "== $g"
  omnigraph snapshot --store "$OLD_ROOT/graphs/$g.omni" | grep -E 'rows=|internal_schema'
done | tee /tmp/baseline.txt
```

Copy it out so it survives the pod — from your workstation, in a second
terminal or after exiting the pod shell:

```shell
kubectl -n omnigraph exec omnigraph-migrate-old -- cat /tmp/baseline.txt \
  > /tmp/baseline.txt
```

### 3. Export every graph with the OLD binary

Still inside `omnigraph-migrate-old`:

```shell
mkdir -p /tmp/export
for g in $(cat /tmp/graph-ids.txt); do
  omnigraph export --store "$OLD_ROOT/graphs/$g.omni" > "/tmp/export/$g.jsonl"
  echo "$g: $(wc -l < /tmp/export/$g.jsonl) lines"
done
```

Then pull them onto your workstation, which is where step 4 pushes them from.
**The image has no `aws` or `curl`**, so the transfer goes through `kubectl`,
not an S3 round trip:

> The image *does* carry `python3` (`python3-minimal` + `python3-yaml`, added
> for the entrypoint's `render_groups.py`) — an earlier version of this note
> said otherwise, which is true of the upstream `modernrelay/omnigraph-server`
> image but not of ours. That is what the automated path's script runs on.

```shell
mkdir -p /tmp/export
for g in $(cat /tmp/graph-ids.txt); do
  kubectl -n omnigraph exec omnigraph-migrate-old -- \
    cat "/tmp/export/$g.jsonl" > "/tmp/export/$g.jsonl"
done
wc -l /tmp/export/*.jsonl
```

Keep that workstation copy until step 7 — it is the only artifact that survives
both pods being deleted. A failure anywhere in this step is a clean stop:
nothing has been written, so scale the Deployment back to 1 and investigate.

### 4. Rebuild at the new root with the NEW binary

> Rehearsed against CI on 2026-08-05 with synthetic rows: `cluster import` →
> `cluster apply` → `load --mode merge` → step 6's verification, on a scratch
> prefix. Row counts round-tripped exactly. What that drill could **not**
> cover is the version gate itself — both pods ran the same binary, because no
> format-bumping image exists yet. Expect the gate's behaviour, and only the
> gate's behaviour, to be new on the day.

This step needs the **new** binary, so it runs in a second pod. Do **all** of
the workstation-side setup first, in one go, and only then open the pod shell —
the rest of the step never leaves it. Interleaving the two is how `$NEW_ROOT`
ends up unset at the moment `sed` uses it.

The image carries the three schema files but **not** `cluster.yaml` — that
exists only inside the *server* pod, where the ConfigMap is mounted over the
same directory. A `kubectl run` pod has no such mount, so the config, the graph
list and the exports all get pushed in from here:

```shell
kubectl -n omnigraph run omnigraph-migrate-new --restart=Never \
  --image=<NEW-image-ref> \
  --overrides='{"apiVersion":"v1","spec":{"serviceAccountName":"omnigraph-server"}}' \
  --env=AWS_REGION=us-east-1 --command -- sleep 86400
kubectl -n omnigraph wait --for=condition=Ready pod/omnigraph-migrate-new --timeout=180s

# graph list
kubectl -n omnigraph exec -i omnigraph-migrate-new -- sh -c 'cat > /tmp/graph-ids.txt' \
  < /tmp/graph-ids.txt

# exports
kubectl -n omnigraph exec omnigraph-migrate-new -- mkdir -p /tmp/export
for g in $(cat /tmp/graph-ids.txt); do
  kubectl -n omnigraph exec -i omnigraph-migrate-new -- \
    sh -c "cat > /tmp/export/$g.jsonl" < "/tmp/export/$g.jsonl"
done

# schemas from the image + cluster.yaml from the ConfigMap
kubectl -n omnigraph exec omnigraph-migrate-new -- \
  sh -c 'mkdir -p /tmp/rebuild && cp /etc/omnigraph/cluster/*.pg /tmp/rebuild/'
kubectl -n omnigraph get configmap omnigraph-cluster-config \
  -o jsonpath='{.data.cluster\.yaml}' \
  | kubectl -n omnigraph exec -i omnigraph-migrate-new -- \
      sh -c 'cat > /tmp/rebuild/cluster.yaml'
```

Now open the shell. **Everything from here to the end of step 4 runs inside
it** — if you do leave, re-set the three roots before doing anything else:

```shell
kubectl -n omnigraph exec -it omnigraph-migrate-new -- sh
```

```shell
ENV=ci                                    # ci | qa | production
OLD_ROOT="s3://ol-data-witan-$ENV"
NEW_ROOT="$OLD_ROOT/fmt5"                 # <-- real digits, not fmt<N>

# Reject an unsubstituted placeholder and a malformed root. `<`/`>` are legal
# in S3 keys, so `fmt<N>` would happily become a real prefix.
case "$NEW_ROOT" in
  *'<'*|*'>'*) echo "NEW_ROOT still has a placeholder — stop"; exit 1 ;;
esac
printf '%s\n' "$NEW_ROOT" | grep -qE '^s3://[a-z0-9.-]+/fmt[0-9]+$' \
  || { echo "NEW_ROOT malformed: $NEW_ROOT — stop"; exit 1; }

omnigraph version        # must show the NEW internal-schema number
```

> The pod's `/bin/sh` is **dash**, not bash — `[[ ... =~ ... ]]` fails with
> `[[: not found`. Every guard in this runbook is POSIX `case`/`grep` for that
> reason; keep it that way if you add more.

Repoint `storage:` with `sed` — there is no `vi`, `vim`, `nano`, or `ed` in the
image, and no `busybox` to fall back on:

```shell
sed -i "s|^storage: .*|storage: $NEW_ROOT|" /tmp/rebuild/cluster.yaml

# storage: must name the new root. An EMPTY value here is the dangerous case.
grep '^storage:' /tmp/rebuild/cluster.yaml
grep -q "^storage: $NEW_ROOT\$" /tmp/rebuild/cluster.yaml \
  && echo "storage repointed OK" || echo "WRONG — do not continue"

grep -c 'schema:' /tmp/rebuild/cluster.yaml    # graph count must be unchanged
omnigraph cluster validate --config /tmp/rebuild
```

> **`cluster validate` does not catch an empty `storage:`.** A cluster.yaml
> with a blank storage value validates clean —
> `cluster config valid: 2 resource(s), 1 dependency edge(s)` — verified
> 2026-08-05. So if `$NEW_ROOT` were unset when `sed` ran, the blanked config
> would sail through validation and `cluster import`/`apply` would build the
> graphs somewhere other than the intended S3 root, with `load` then reporting
> success. That is why the `grep -q` above exists: validation is not the guard
> here.

What validation *does* catch is an unresolvable `schema:` reference and
structural damage. It reports `N resource(s), M dependency edge(s)` — two
resources and one edge per declared graph, so 16 graphs give
`32 resource(s), 16 dependency edge(s)`. A count that does not match the graph
list means the `sed` ate more than the storage line.

Now bootstrap the new root's state ledger, **then** converge it. Both commands,
in this order:

```shell
omnigraph cluster import --config /tmp/rebuild --as svc-witan-admin
omnigraph cluster apply  --config /tmp/rebuild --as svc-witan-admin
```

> **`cluster apply` alone fails on a root that has never been applied to.** A
> new storage root has no `__cluster/state.json`, and apply refuses rather than
> creating one:
>
> ```
> ERROR state_missing __cluster/state.json: apply requires an existing
> state.json; run `cluster import` to bootstrap state
> ```
>
> `cluster import` writes that ledger — at a fresh root it observes nothing and
> records revision 1 with 0 resources, which is exactly right. `apply` then
> creates the graphs and moves to revision 2. This bites only on the *first*
> apply against a new root, which is precisely what this procedure does, at the
> point of no return, during an outage.

Expect apply to report `Create graph.<id>` and `Create schema.<id>` for every
declared graph, ending `converged: true, written: true`. Then load each export:

```shell
for g in $(cat /tmp/graph-ids.txt); do
  omnigraph load --store "$NEW_ROOT/graphs/$g.omni" \
    --data "/tmp/export/$g.jsonl" --mode merge --yes
done
```

`--mode merge` into a freshly-created empty graph is equivalent to a full load
and is the safe choice — `overwrite` is destructive and buys nothing here.
`--yes` is required because a non-local destructive write refuses without a TTY.

### 5. Repoint the cluster and deploy the new image

Set the prefix — the same one `$NEW_ROOT` names, without the `s3://<bucket>/`:

```shell
cd src/ol_infrastructure/applications/omnigraph
pulumi config set omnigraph:storage_prefix fmt5 --stack <CI|QA|Production>
```

A malformed value fails the preview rather than deploying: wrapping slashes and
anything outside a single `[A-Za-z0-9._-]` segment are rejected, which includes
an unsubstituted `fmt<N>`. That check lives here because it cannot live
downstream — `cluster validate` accepts any storage string, empty ones
included.

Confirm the generated config before applying:

```shell
pulumi preview --stack <CI|QA|Production> --diff | grep -E '^\s*[-+]?\s*storage:'
```

Expect a `-`/`+` pair showing the old root replaced by the new one. **Empty
output means the edit did not take** — the ConfigMap is unchanged and the deploy
will no-op back onto the old root. Stop and fix the edit rather than applying.

If the diff is hard to read (the ConfigMap `data` renders as one blob), fall
back to the generated value itself:

```shell
pulumi preview --stack <CI|QA|Production> --json \
  | jq -r '.steps[]?.newState?.inputs?.data?["cluster.yaml"] // empty' \
  | grep '^storage:'
```

Then let the paused pipeline's deploy job for this environment run (or
`pulumi up` directly with `OMNIGRAPH_DOCKER_SHA` set to the new digest).

The deploy regenerates cluster.yaml against the new root, runs its own
`cluster apply` Job — a no-op, since step 4 already converged that root — and
starts the Deployment on the rebuilt graphs. The pod-template config hash
changes with cluster.yaml, so the restart happens on its own.

### 6. Verify before releasing the outage

A graph that opens cleanly while missing half its rows looks perfectly healthy
to `/healthz`, so the row-count diff — every graph, not a spot check — is the
check that matters. From your workstation, with the server back up:

```shell
kubectl -n omnigraph exec deploy/omnigraph-server -- omnigraph version

for g in $(cat /tmp/graph-ids.txt); do
  echo "== $g"
  kubectl -n omnigraph exec deploy/omnigraph-server -- \
    omnigraph snapshot --store "$NEW_ROOT/graphs/$g.omni" \
    | grep -E 'rows=|internal_schema'
done | tee /tmp/after.txt
```

Two comparisons against the step 2 baseline, and they expect **opposite**
answers — which is why this is not one plain `diff`:

```shell
# 1. Row counts must be identical. Empty output = pass.
diff <(grep -E '^==|rows=' /tmp/baseline.txt) \
     <(grep -E '^==|rows=' /tmp/after.txt) && echo "row counts match"

# 2. The format version must have MOVED, uniformly, on every graph.
grep internal_schema /tmp/after.txt | sort -u
```

The second must print exactly one line, carrying the new binary's
`internal-schema` number. More than one line means some graph did not get
rebuilt; the old number means you are still serving the old root — go back to
step 5 and check the `pulumi preview --diff`.

Finish by exercising a real client path (a `recall`, a `task_ready`) rather
than trusting probes.

### 7. Clean up, then retire the old root

Delete the workspace pods once verification passes — they hold an idle
`sleep 86400` on the `omnigraph-server` ServiceAccount, and leaving them around
is a stray identity with write access to the bucket:

```shell
kubectl -n omnigraph delete pod omnigraph-migrate-old omnigraph-migrate-new --ignore-not-found
```

**Resume the maintenance sweeps.** Clearing the migration config does both —
it removes the Job and un-suspends `optimize`/`cleanup` in one `pulumi up`:

```shell
pulumi config rm omnigraph:migrate_from_image --stack <CI|QA|Production>
pulumi config rm omnigraph:migrate_to_prefix  --stack <CI|QA|Production>
pulumi up --stack <CI|QA|Production>
kubectl -n omnigraph get cronjob    # SUSPEND must read False for both
```

Left suspended, the first symptom is fragment bloat degrading query latency
weeks later, with nothing pointing back at the migration that caused it. If you
suspended by hand instead, un-patch by hand.

The old root itself waits. **Not the same day** — leave `$OLD_ROOT/graphs/`
untouched through at least one full soak (a week for Production), because it is
the only fast rollback. Delete it, and the workstation copy of the exports in
`/tmp/export/`, only after the next environment has migrated successfully too.

## Rollback

Before step 5 there is nothing to roll back: scale to 1 and you are on the old
image against the old root.

After step 5:

```shell
pulumi config rm omnigraph:storage_prefix --stack <CI|QA|Production>
```

then redeploy with `OMNIGRAPH_DOCKER_SHA` pinned to the **old** image digest.
Confirm with the same `pulumi preview --diff` check that `storage:` is back to
the derived root before applying.

The old root was never written to, so this is a revert, not a restore. Writes
that landed on the new root after step 5 are lost — which is the real reason
step 6 happens before you tell anyone the service is back.

## Troubleshooting

**The rollout fails on the version gate and you have not migrated.** Expected —
this is the failure this runbook exists for, and the store is undamaged. Pause
the pipeline so the image stops promoting, redeploy the old digest to restore
service, then schedule the migration.

**`cluster apply` fails holding a state lock.** The state ledger
(`<root>/__cluster/state.json`) is locked; `cluster apply` is single-writer by
design. If a previous attempt died mid-run, clear it deliberately — the lock id
comes from `cluster status` or from the `state_lock_held` diagnostic, and is a
required argument:

```shell
omnigraph cluster status --config /tmp/rebuild
omnigraph cluster force-unlock <LOCK_ID> --config /tmp/rebuild --as svc-witan-admin
```

**A `code-<repo>` graph is missing after the rebuild.** Its id is derived by
`code_graph_id()` in `data_tier.py`, mirroring `witan_code.config.graph_id` in
agent-kit. Enumerating from `managed_repos` instead of from the live cluster.yaml
will drop any repo whose normalization you got wrong. Re-check against the
ConfigMap listing in *What gets rebuilt*.

**Row counts do not match.** Do not release the outage. The exports are in
`/tmp/export/` on your workstation and the old root is intact — roll back and
reconcile offline. A partial load is the one outcome worse than a failed one,
because it looks like success.

## Not needed for

- an ordinary image bump at the same format — `Recreate` handles it;
- a **schema** change (adding a field to `schema.pg`) — the pre-deploy
  `cluster apply` Job converges those on every deploy;
- adding a managed repo — a cluster.yaml change the same Job handles.

## References

- [ADR-0009](adr/0009-deploy-witan-as-shared-multi-tenant-mcp-service.md) — the
  two-tier deployment this operates on; see its storage-format addendum.
- `src/ol_infrastructure/applications/omnigraph/data_tier.py` — the Deployment,
  `Recreate` strategy, cluster.yaml generation, and `cluster apply` Job.
- `src/ol_infrastructure/applications/omnigraph/storage_migration.py` — the
  migration Job, its initContainer, and why it stops short of the cutover.
- `src/ol_infrastructure/applications/omnigraph/scripts/migrate_storage_format.py`
  — what the Job actually runs: baseline, export, rebuild, verify.
- `src/ol_infrastructure/applications/omnigraph/maintenance.py` — the two
  sweeps and the `suspend` flag a migration sets.
- `src/ol_concourse/pipelines/infrastructure/omnigraph/pipeline.py` — the
  build-and-promote chain to pause.
- [witan token sync runbook](witan-token-sync-runbook.md) — the other
  operational path that restarts this Deployment.
