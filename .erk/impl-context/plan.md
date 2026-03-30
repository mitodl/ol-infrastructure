# Plan 1: code-graph-rag Infrastructure (ol-infrastructure)

## Scope

Hosted infrastructure for code-graph-rag plus the shared MCP platform (ToolHive) that will serve as the central MCP service registry for all team tooling. Developer tooling, `cgr` CLI, skills, and developer-facing MCP configs belong in [Plan 2: ol-cli](./plan-ol-cli.md).

---

## Architecture

```
infrastructure.toolhive.Production
  ├── ToolHive K8s Operator (MCPServer CRD controller)
  ├── ToolHive Registry Server (auto-discovers MCPServer resources in cluster)
  └── vMCP Gateway (single /mcp endpoint aggregating all MCPServer resources)

infrastructure.memgraph.codegraph.QA/Production
  └── outputs: bolt_endpoint, namespace, secret_name

applications.code_graph_rag.QA/Production
  ├── StackReference → infrastructure.memgraph.codegraph.<env>
  └── MCPServer CRD { image: code-graph-rag, env: MEMGRAPH_HOST=... }
        └── managed by ToolHive Operator → Deployment + Service + auto-registered in Registry
```

**Multi-tenancy:** Memgraph has one graph per instance; code-graph-rag uses `Project` nodes for per-repo isolation. CI indexing uses `cgr start --update-graph` CLI (Project-aware, incremental). ⚠️ MCP `index_repository` tool wipes all data — never used in CI.

---

## Phase 1: ToolHive Infrastructure Stack

### `src/ol_infrastructure/infrastructure/toolhive/`

New reusable stack for the team's MCP management platform. Pattern follows `infrastructure/qdrant_cloud/`.

**`Pulumi.yaml`**:
```yaml
name: ol-infrastructure-toolhive
runtime: python
description: ToolHive MCP platform — Operator, Registry Server, vMCP Gateway
backend:
  url: s3://mitol-pulumi-state/
```

**`__main__.py`** creates:

1. **ToolHive Operator** via `kubernetes.helm.v3.Release`:
   - `oci://ghcr.io/stacklok/toolhive/toolhive-operator-crds` (CRDs)
   - `oci://ghcr.io/stacklok/toolhive/toolhive-operator` (operator)
   - Namespace: `toolhive-system`

2. **ToolHive Registry Server** via `kubernetes.helm.v3.Release` or raw manifests:
   - Configured with **Kubernetes Cluster source** → auto-discovers `MCPServer` resources across the cluster
   - PostgreSQL backend (or SQLite for smaller deployments) — stack config: `toolhive:db_type`
   - Service + optional Ingress for developer access to the registry API / Portal

3. **Vault VSO Secret** for ToolHive admin credentials (existing VSO pattern)

**Stack outputs:** `registry_url`, `vmcp_gateway_url`, `operator_namespace`

Stack naming: `infrastructure.toolhive.Production`

---

## Phase 2: Memgraph Infrastructure Stack

### `src/ol_infrastructure/infrastructure/memgraph/`

Reusable stack — each logical Memgraph deployment gets its own named stack:
- `infrastructure.memgraph.codegraph.QA/Production` — for code-graph-rag
- `infrastructure.memgraph.knowledge.Production` — future knowledge graph use

**`Pulumi.yaml`**:
```yaml
name: ol-infrastructure-memgraph
runtime: python
description: Managed Memgraph graph database deployment via Helm on EKS
backend:
  url: s3://mitol-pulumi-state/
```

**`__main__.py`** creates:
- `kubernetes.helm.v3.Release` — Memgraph standalone Helm chart (`memgraph/memgraph`), pinned version
- `PersistentVolumeClaim` — `ebs-gp3`, size from `memgraph:storage_size`
- Vault VSO `VaultStaticSecret` — Memgraph credentials (existing pattern)
- Optional Memgraph Lab `Deployment` + `Service` (`memgraph:enable_lab`)
- Headless `Service` for Bolt (7687)
- `NetworkPolicy` — Bolt access from `memgraph:allowed_namespaces` only

**Stack outputs:** `bolt_endpoint`, `http_endpoint`, `namespace`, `secret_name`

**Stack config** (`Pulumi.infrastructure.memgraph.codegraph.QA.yaml`):
```yaml
secretsprovider: awskms://alias/infrastructure-secrets-qa
config:
  memgraph:namespace: code-intelligence
  memgraph:storage_size: 50Gi
  memgraph:memory_limit: 8Gi
  memgraph:enable_lab: "true"
  memgraph:allowed_namespaces: ["code-intelligence", "toolhive-system"]
  memgraph:helm_version: "0.3.0"
```

---

## Phase 3: code-graph-rag Application Stack

### `src/ol_infrastructure/applications/code_graph_rag/`

**`Pulumi.yaml`**:
```yaml
name: ol-infrastructure-code-graph-rag
runtime: python
description: code-graph-rag MCP server — managed by ToolHive Operator
backend:
  url: s3://mitol-pulumi-state/
```

**`__main__.py`**:
- `StackReference` to `infrastructure.memgraph.codegraph.<env>` — consumes `bolt_endpoint`, `secret_name`
- `StackReference` to `infrastructure.toolhive.Production` — consumes `operator_namespace`
- Vault VSO `VaultStaticSecret` for LLM API keys (CYPHER_PROVIDER credentials)
- **`MCPServer` CRD resource** (ToolHive Operator manages the rest):
  ```yaml
  apiVersion: toolhive.stacklok.com/v1alpha1
  kind: MCPServer
  metadata:
    name: code-graph-rag
    namespace: toolhive-system
  spec:
    image: ghcr.io/vitali87/code-graph-rag:latest
    transport: sse        # or streamable-http
    port: 8080
    env:
      - name: MEMGRAPH_HOST
        value: <bolt_endpoint host>
      - name: MEMGRAPH_PORT
        value: "7687"
      - name: CYPHER_PROVIDER
        valueFrom:
          secretKeyRef:
            name: <secret_name>
            key: cypher_provider
  ```
- ToolHive Operator auto-creates: Deployment, Service, registers in Registry Server
- ⚠️ Comment in code: `index_repository` MCP tool wipes data; indexing via Concourse only

Stack naming: `applications.code_graph_rag.QA` / `applications.code_graph_rag.Production`

---

## Phase 4: Concourse Indexing Pipeline

### `src/ol_concourse/pipelines/code_intelligence/index_repos.py`

- Triggered on push to main for each configured repo
- Task: `cgr start --update-graph --repo-path <path>` (Project-aware, incremental, safe for shared graph)
- Parameterized repo list in YAML config
- Bolt endpoint from Memgraph stack output (via Vault or Concourse config)
- Failure notification

---

## Files to Create

| File | Purpose |
|------|---------|
| `src/ol_infrastructure/infrastructure/toolhive/Pulumi.yaml` | Project definition |
| `src/ol_infrastructure/infrastructure/toolhive/__main__.py` | ToolHive Operator + Registry Server + vMCP Gateway |
| `src/ol_infrastructure/infrastructure/toolhive/Pulumi.infrastructure.toolhive.Production.yaml` | Production stack config |
| `src/ol_infrastructure/infrastructure/memgraph/Pulumi.yaml` | Project definition |
| `src/ol_infrastructure/infrastructure/memgraph/__main__.py` | Helm + PVC + VSO secret + NetworkPolicy + Lab |
| `src/ol_infrastructure/infrastructure/memgraph/Pulumi.infrastructure.memgraph.codegraph.QA.yaml` | QA stack config |
| `src/ol_infrastructure/infrastructure/memgraph/Pulumi.infrastructure.memgraph.codegraph.Production.yaml` | Production stack config |
| `src/ol_infrastructure/applications/code_graph_rag/Pulumi.yaml` | Project definition |
| `src/ol_infrastructure/applications/code_graph_rag/__main__.py` | MCPServer CRD + StackReferences + Vault secrets |
| `src/ol_concourse/pipelines/code_intelligence/index_repos.py` | Indexing pipeline |

---

## Todos

1. Create ToolHive infrastructure stack (`infrastructure/toolhive/`)
2. Create Memgraph infrastructure stack (`infrastructure/memgraph/`)
3. Create Memgraph stack YAML configs (codegraph.QA + codegraph.Production)
4. Create code-graph-rag application stack with MCPServer CRD (`applications/code_graph_rag/`)
5. Create Concourse indexing pipeline
