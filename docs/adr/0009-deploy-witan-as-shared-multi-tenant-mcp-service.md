# 0009. Deploy witan as a Shared, Multi-Tenant MCP Service

**Status:** Accepted
**Date:** 2026-07-07
**Deciders:** Tobias Macey (agent-assisted scoping session)
**Technical Story:** Scoping effort to move `witan` (agent-kit repo) from a per-developer local tool to a shared, deployed, multi-tenant service, spanning the `agent-kit` and `ol-infrastructure` repositories.

## Context

### Current Situation

`witan` ([agent-kit](https://github.com/mitodl/agent-kit) repo) is a Python FastMCP
server backed by an `omnigraph`/Lance graph-file store, providing shared team
memory, task coordination, and per-repo code graphs to coding agents. Today it
runs exclusively as a per-developer local process — `uvx`/`uv run` speaking MCP
over stdio, against a local `.omni` file store per machine. There is no shared
team memory, no shared task-coordination graph visible across machines, and no
way for CI or a second machine to see what another session already learned.

### Problem Statement

How do we move witan from a local-only tool to a shared, network-reachable,
multi-tenant service, without hand-building an authentication/ingress/hosting
layer from scratch, while preserving reasonable authorization and
write-isolation guarantees?

### Business/Technical Drivers

- Cross-team memory/pattern discovery only works if there's one shared graph,
  not N per-developer copies — that's the entire point of the work graph.
- CI and automated agents need to read/write the same task-coordination graph
  humans use, which a local-only store can't provide.
- Multiple people/agents will write concurrently once shared; today's
  client-side advisory lock (`fcntl.flock`) is local-file-only.

### Constraints

- omnigraph's storage engine (Lance) embeds absolute paths in its files —
  stores can't be relocated with `mv`, only `export`/`load`.
- witan's remote-store code path (`graph_uri=http://…` / `s3://…`) already
  exists in the client (`witan/config.py:17-21`) but has never been run
  against a real `omnigraph-server` instance.
- ol-infrastructure already runs a cluster-scoped ToolHive (Stacklok) operator
  on the `operations` EKS cluster (`src/ol_infrastructure/applications/toolhive_operator/`),
  serving one existing consumer (`toolhive_swe`, hosting a stateless `fetch`
  MCP server behind a `VirtualMCPServer` with Keycloak-brokered OAuth). Any new
  hosting decision has to account for this existing investment rather than
  duplicate it.

### Assumptions

- `omnigraph-server` serializes concurrent writers on its own — the client
  already skips its local lock for remote URIs (`witan/graph.py:169-177`) on
  this assumption. **Unverified** — flagged below as a required pre-launch
  spike, not treated as given.
- Coarse, per-team (not per-individual-human) authorization is acceptable for
  a v1 launch.

### Options Considered

1. **Hand-rolled ingress + `omnigraph-server`, no ToolHive**
   - Pros: no dependency on ToolHive's maturity/roadmap; full control over
     every layer.
   - Cons: reimplements the OIDC broker, ingress, and MCP lifecycle/registry
     that `toolhive_swe` already solved for this cluster; materially more
     surface area to build and operate for no clear benefit.

2. **Direct S3 `graph_uri` clients, no server process at all**
   - Pros: simplest possible topology — no new service to run.
   - Cons: pushes all authorization onto S3 bucket IAM, which cannot express
     "user X can write `Memory` nodes tagged `repo=agent-kit` but not `Task`
     nodes." No way to layer omnigraph's Cedar policy engine on top at all.

3. **Reuse the existing ToolHive operator for the MCP tier, a separate
   `omnigraph-server` for the data tier (chosen)**
   - Pros: reuses already-running, already-proven infrastructure (cluster-scoped
     operator, Keycloak OIDC broker, APISIX TLS termination); the only new
     work is a namespace/stack plus the data-tier process itself.
   - Cons: ToolHive's `MCPServer` CRD has no demonstrated PVC-mount support
     for the tool-server pod, so the data tier must be a separate Deployment
     wired by network address — one more moving part, though this exactly
     mirrors the existing Redis-behind-vMCP pattern `toolhive_swe` already
     uses for its own stateful backend.

## Decision

**Deploy witan as two separate workloads on the existing shared ToolHive
operator infrastructure:**

1. **MCP tier** — witan's FastMCP process, converted to `streamable-http`
   transport, registered as a new `MCPServer` in a new `toolhive_witan`
   namespace/Pulumi stack, following the `toolhive_swe` pattern
   (Keycloak-brokered OAuth via ToolHive's embedded auth server, APISIX
   TLS-only passthrough). No new operator instance — the existing operator is
   already cluster-scoped and reconciles `MCPServer` resources in any
   namespace.
2. **Data tier** — `omnigraph-server` runs as its own stateless Deployment
   against an S3-backed store (`graph_uri=s3://…`, IRSA-scoped bucket via the
   existing `OLBucket`/`S3BucketConfig` component). One shared Layer-1
   (memory/task/workflow) graph, organization-wide; per-repo code graphs
   hosted off the same server process via its existing multi-graph
   (`omnigraph graphs`) management. The MCP tier talks to the data tier over
   the cluster network only — it is never exposed directly — mirroring the
   Redis-behind-vMCP precedent, except S3-backed so the data-tier pod itself
   needs no PVC/StatefulSet.
3. **Authorization** — enforced by omnigraph's built-in Cedar policy engine,
   not by ToolHive (which is authentication-only today: any Keycloak-authenticated
   user gets full access to the aggregated tool set, per the existing
   `toolhive_swe` model). v1 uses coarse principals bound to per-team/CI
   bearer tokens (`svc-witan-<team>`, `svc-witan-ci`, `svc-witan-admin`)
   rather than per-individual-human tokens, because omnigraph binds actor
   identity to the bearer token **server-side** for remote access — dynamic
   per-user actor switching (`--as <ACTOR>`) only works for local/direct-engine
   access. Per-individual identity via a ToolHive OIDC→omnigraph token-exchange
   bridge is a real future capability (ToolHive documents support for
   [RFC 8693 OAuth 2.0 Token Exchange](https://datatracker.ietf.org/doc/html/rfc8693))
   but is explicitly deferred past v1.
4. **Code-graph branching** (per-user/per-branch WIP isolation) is already
   implemented in `witan-code` — no new mechanism needed. The remaining work
   is a Cedar policy restricting writes to `main` to the `svc-witan-ci`
   principal, and a stale-branch reaper.

### Rationale

Every alternative to reusing ToolHive means rebuilding infrastructure (OIDC
broker, ingress, lifecycle management) that already exists and is already
proven on this cluster for exactly this purpose — hosting MCP servers for a
bounded set of internal teams. The only genuinely new infrastructure is the
data tier, and an S3-backed design keeps even that to a plain stateless
Deployment rather than a StatefulSet+PVC.

Full technical detail (Cedar policy bundle contents, migration runbook,
remaining open questions) is intentionally not duplicated here and is tracked
as follow-up implementation work in the `agent-kit` repository, since that
detail belongs to implementation, not the architectural decision itself.

## Consequences

### Positive Consequences

- Reuses proven, already-running infrastructure instead of building a
  parallel MCP-hosting stack.
- Cross-team memory/task discovery becomes possible for the first time.
- Cedar gives real, policy-driven read/write scoping in place of
  "any authenticated user gets everything" — today's `toolhive_swe` auth
  model.
- Code-graph branch isolation (already shipped) plus CI-gated `main` writes
  means concurrent WIP reindexing can't corrupt the shared graph.

### Negative Consequences

- Introduces a second workload (the data tier) per witan deployment, beyond
  what the ToolHive operator manages natively — one more thing to operate and
  monitor.
- v1 authorization granularity is per-team, not per-individual — a
  compromised or overly-broad team token grants more access than strictly
  necessary until the token-exchange bridge is built.
- `omnigraph-server`'s actual write-serialization behavior under concurrent
  remote writers is unverified; if it doesn't serialize as assumed, the
  storage design (S3-backed, no server-side lock demonstrated in this
  environment) needs rework before launch.
- Depends on a separate fix to witan's task-claim mechanism (today's
  claim/release is read-check-write, not an atomic compare-and-swap) landing
  before the task-coordination graph is safe for concurrent multi-agent use.

### Neutral Consequences

- Plain local `uvx`/`uv run` usage remains fully supported and is still the
  right mode for private scratch work — this decision adds a second
  deployment mode, it doesn't replace the first.
- Observability for this class of service doesn't exist yet in either repo —
  greenfield follow-up work, not a blocker for a first internal pilot.

## Implementation Notes

- **Effort Estimate:** Multi-week — spans a concurrency-behavior spike, witan
  transport work, Cedar policy authoring, and a new Pulumi stack. Broken into
  a prioritized task backlog (see Related Decisions).
- **Risk Level:** Medium — no single piece is exotic, but the
  write-serialization assumption and the coarse-authorization tradeoff above
  are real open risks, not resolved by this ADR.
- **Dependencies:** atomic (compare-and-swap) task claims landing in witan, a
  completed remote-write serialization spike, streamable-http transport
  support landing in witan.
- **Migration Path:** existing local `.omni` stores move to the shared graph
  via `omnigraph export` → `init --schema` on the target → `load --mode merge`
  — never `mv`/copy the Lance files directly, since they embed absolute
  paths.

## Related Decisions

- [ADR-0003](0003-use-hybrid-httproute-apisixtls-for-per-app-tls.md) — Use
  Hybrid HTTPRoute + ApisixTls for Per-App TLS — reused as-is for the new
  `toolhive_witan` ingress.
- [ADR-0005](0005-high-performance-stateful-applications-eks.md) — High
  Performance Stateful Applications on EKS — informed the choice of an
  S3-backed data tier over an EBS-backed StatefulSet, since the `MCPServer`
  CRD has no demonstrated path to mount a PVC into the tool-server pod.
- `toolhive_operator` / `toolhive_swe` (existing infra, predates this ADR
  series' coverage of ToolHive) — the operator/ingress/OIDC-broker pattern
  this decision reuses wholesale.
- Full technical scoping, the Cedar policy bundle design, and the prioritized
  implementation backlog are tracked separately in the `agent-kit` repository
  as follow-up engineering work, not duplicated here.

## References

- [Stacklok ToolHive docs](https://docs.stacklok.com/toolhive/)
- [Cedar policy language](https://www.cedarpolicy.com/)

## Notes

Produced during an agentic scoping session. Originally drafted as a
standalone RFC in agent-kit's `docs/design/`; converted to this
ADR to match ol-infrastructure's established decision-record process, since
the core decision here is fundamentally an infrastructure/deployment one
spanning both repos.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2026-07-07 | _Pending_ | _Pending_ | Created during agentic scoping session |
| 2026-07-07 | Tobias Macey | Approved | Accepted after Copilot automated review feedback addressed (RFC citation, ADR index, self-containment) |

**Last Updated:** 2026-07-07
