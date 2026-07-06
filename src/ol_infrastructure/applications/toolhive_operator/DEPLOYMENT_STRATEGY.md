# ToolHive Deployment Strategy

Notes on environment management, layer separation, and single-vs-scoped
deployment of ToolHive in `ol-infrastructure`. Written to answer review feedback
on the initial ToolHive deployment PR (#4837).

## The question, restated as two independent axes

Review feedback bundled two different questions together. Separating them makes
the answer much clearer:

1. **Environment separation** (CI / QA / Production) — *the same ToolHive,
   different deploy targets.*
2. **Workload separation** (software-engineering agents vs application agents vs
   data agents) — *different consumers within one environment.*

These have different right answers, and the repo already provides the tools for
both.

## The technical constraint that drives everything

ToolHive has three layers, and they do not all behave the same way:

| Layer | K8s scope | Can you have >1 per cluster? |
|---|---|---|
| **CRDs** (`MCPServer`, etc.) | Cluster-scoped object | **No** — one CRD *version* per cluster |
| **Operator** (control plane) | Cluster- or namespace-scoped (`operator.rbac.scope`) | Yes (namespace-scoped operators can coexist) |
| **MCPServer workloads** (the proxies) | Namespaced | Yes — as many namespaces as you want |

The decisive fact: **CRDs are a cluster singleton.** So "multiple ToolHive
installations in one cluster" can only mean *multiple namespace-scoped operators
sharing one set of CRDs*. True isolation (separate CRD versions, separate control
planes) only comes from **separate clusters** — which this repo already has.

## Axis 1: Environment management → use the existing cluster model

We do not separate CI/QA/Prod inside a cluster. `ol-infrastructure` already runs
**separate EKS clusters per environment** — `Pulumi.operations.CI.yaml`,
`.QA.yaml`, `.Production.yaml`, mirrored for the `applications`, `data`, and
`residential` clusters. Keycloak and kubewatch follow this pattern.

So ToolHive's operator + CRDs deploy **per environment cluster**, identical to
Keycloak. `__main__.py` already keys off `stack_info` and stack-references
`operations.{stack_info.name}`, so it is environment-agnostic by construction.
Today only `Pulumi.CI.yaml` exists; promoting to QA/Prod is **config-only** (add
the two stack files, add `toolhive` to those clusters' `eks:namespaces`).

**Takeaway:** environment separation is not special-cased — it falls out of the
existing per-environment cluster split.

## Axis 2: One installation or scoped per agent class?

**Recommendation: one cluster-scoped operator per cluster, and separate the agent
classes by namespace — not by separate operators or clusters.** Start there;
escalate only when a hard requirement forces it.

A namespace already provides everything a "scoped deployment" is usually meant to
buy:

- **RBAC** boundary per agent class
- **NetworkPolicy** (which agents can reach which MCP servers)
- **ResourceQuota / LimitRange** (data agents cannot starve SWE agents)
- **IRSA** — distinct service-account → AWS-role mapping per class

So `toolhive-swe`, `toolhive-apps`, `toolhive-data` namespaces, with one operator
reconciling all of them, is the right starting point: one control plane to
patch/upgrade, one CRD set to version, real isolation where it counts.

### When to actually split further (and into what)

- **Different CRD/operator versions per class** → you *must* use separate clusters
  (CRD singleton). Rare.
- **An MCP server needs IRSA into the data or applications account/VPC** (e.g. a
  data-agent MCP server hitting Redshift/RDS without cross-account hops) → run
  *that class's* MCPServers on the `data` / `applications` cluster instead of
  `operations`. This maps onto the existing taxonomy: application agents ↔
  `applications`, data agents ↔ `data`, SWE/platform agents ↔ `operations`.
- **Hard multi-tenancy / compliance isolation** → separate clusters.

### Centralize vs distribute

The deciding question is: **what do the MCP servers need to reach?** MCP servers
are network-reachable backends, and agent *clients* can call them cross-cluster.

- Generic servers (web search, docs, GitHub) → **centralize** on `operations`
  with namespace separation.
- A server needing in-cluster/in-account access to a specific data plane → push
  *that one* server to the cluster that owns that data plane. Do not move
  everything.

## Concrete plan for the current PR

This is an initial bootstrap, so it should not be over-built — but the two axes
should be explicit so the structure scales:

1. **Keep it on `operations`, keep the operator cluster-scoped.** That is the
   right home for a shared control plane.
2. **Document that environment separation = the per-env cluster model** (QA/Prod
   are follow-up config-only stacks), so it reads as deliberate, not missing.
3. **Parameterize the agent-class namespace** rather than hardcoding `toolhive`
   for workloads — keep the operator/CRDs in `toolhive`, but plan MCPServers into
   `toolhive-{class}` namespaces. Naming the convention now answers the "scoped
   deployments" question.
4. **Defer multi-operator / multi-cluster** explicitly, with the trigger
   conditions above ("we split a class onto its own cluster *if* it needs IRSA
   into that account, or *if* it needs a different operator version").

## One-line summary

Environments separate by **cluster** (already solved); agent classes separate by
**namespace** under one operator (recommended); a class graduates to its own
operator/cluster only when CRD-version or account-level IRSA isolation demands it.
