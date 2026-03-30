"""code-graph-rag MCP server application stack.

Stack naming: applications.code_graph_rag.QA / applications.code_graph_rag.Production

This stack provisions:
- Vault VSO VaultStaticSecret for LLM API keys (CYPHER_PROVIDER / ORCHESTRATOR_PROVIDER)
- ToolHive MCPServer CRD resource — the operator auto-creates the Deployment + Service
  and registers the server in the ToolHive Registry

The ToolHive Operator (from infrastructure.toolhive.appmcps.<env>) watches
for MCPServer resources and reconciles them into Deployments.
code-graph-rag is classified as an agent-facing MCP (applications cluster) since it
provides code intelligence to agentic workloads.

⚠️  IMPORTANT: Do NOT use the `index_repository` MCP tool against the shared hosted
    Memgraph instance. It wipes all project data for the given project_name. Repository
    indexing is handled exclusively by the Concourse pipeline using:
        cgr start --update-graph --repo-path <path>
    which is Project-aware and incremental (uses .cgr_hash_cache).

Stack references:
  infrastructure.memgraph.codegraph.<env>  → bolt_host, bolt_port, namespace
  infrastructure.toolhive.appmcps.<env>    → operator_namespace
"""

import pulumi_kubernetes as kubernetes
from pulumi import Config, ResourceOptions, StackReference, export

from ol_infrastructure.components.services.vault import (
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.eks_helper import setup_k8s_provider
from ol_infrastructure.lib.ol_types import AWSBase
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
setup_vault_provider(stack_info)
cgr_config = Config("code_graph_rag")
vault_config = Config("vault")

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
aws_config = AWSBase(tags={"OU": "operations", "Environment": stack_info.env_suffix})

k8s_global_labels: dict[str, str] = {
    "ol.mit.edu/stack": stack_info.full_name,
    "ol.mit.edu/managed-by": "pulumi",
    "ol.mit.edu/application": "code-graph-rag",
    "ol.mit.edu/service": "mcp-server",
}

# Stack references
cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
memgraph_stack = StackReference(f"infrastructure.memgraph.codegraph.{stack_info.name}")
toolhive_stack = StackReference(f"infrastructure.toolhive.appmcps.{stack_info.name}")
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

memgraph_bolt_host = memgraph_stack.get_output("bolt_host")
memgraph_bolt_port = memgraph_stack.get_output("bolt_port")
memgraph_namespace = memgraph_stack.get_output("namespace")
toolhive_namespace = toolhive_stack.get_output("operator_namespace")

# Namespace for code-graph-rag resources (same as Memgraph for network access)
cgr_namespace_name: str = cgr_config.get("namespace") or "code-intelligence"

cgr_namespace = kubernetes.core.v1.Namespace(
    "code-graph-rag-namespace",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=cgr_namespace_name,
        labels={
            **k8s_global_labels,
            "kubernetes.io/metadata.name": cgr_namespace_name,
        },
    ),
)

# LLM provider configuration from stack config.
# CYPHER_PROVIDER generates Cypher queries from natural language (needs to be accurate).
# ORCHESTRATOR_PROVIDER drives the agentic code-graph reasoning loop.
# Both default to "ollama" for free local inference; override in stack YAML for LLMs.
cypher_provider: str = cgr_config.get("cypher_provider") or "ollama"
orchestrator_provider: str = cgr_config.get("orchestrator_provider") or "ollama"
cypher_model: str = cgr_config.get("cypher_model") or "codellama:7b"
orchestrator_model: str = cgr_config.get("orchestrator_model") or "codellama:7b"

# VaultStaticSecret for LLM API keys — only required when using a hosted LLM provider.
# The secret at vault path code-graph-rag/<env> should contain:
#   cypher_api_key, orchestrator_api_key (base64 values if using external providers)
llm_secret_name = f"code-graph-rag-llm-{stack_info.env_suffix}"
vault_mount = f"secret-{stack_info.env_prefix}"

llm_vault_secret = OLVaultK8SSecret(
    f"code-graph-rag-llm-secret-{env_name}",
    OLVaultK8SStaticSecretConfig(
        name=f"code-graph-rag-llm-{stack_info.env_suffix}",
        namespace=cgr_namespace_name,
        dest_secret_name=llm_secret_name,
        dest_secret_labels=k8s_global_labels,
        labels=k8s_global_labels,
        mount=vault_mount,
        mount_type="kv-v2",
        path="code-graph-rag",
        templates={
            "CYPHER_API_KEY": '{{ get .Secrets "cypher_api_key" }}',
            "ORCHESTRATOR_API_KEY": '{{ get .Secrets "orchestrator_api_key" }}',
        },
        refresh_after="1h",
        vaultauth=vault_stack.get_output("vault_k8s_auth_backend").apply(
            lambda auth: f"{auth}-code-graph-rag"
        ),
    ),
    opts=ResourceOptions(
        parent=cgr_namespace,
        depends_on=[cgr_namespace],
        delete_before_replace=True,
    ),
)

# MCPServer CRD — ToolHive Operator reconciles this into a Deployment + Service
# and registers it in the Registry Server automatically.
#
# MCPServer placed in toolhive-appmcps namespace (agent-facing installation on
# the applications cluster). The ToolHive Operator in that namespace reconciles
# this resource into a Deployment + Service and registers it in the Registry.
mcpserver = kubernetes.apiextensions.CustomResource(
    "code-graph-rag-mcpserver",
    api_version="toolhive.stacklok.com/v1alpha1",
    kind="MCPServer",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="code-graph-rag",
        namespace=toolhive_namespace,
        labels=k8s_global_labels,
    ),
    spec={
        "image": "ghcr.io/vitali87/code-graph-rag:latest",
        "transport": "sse",
        "port": 8080,
        "env": [
            {
                "name": "MEMGRAPH_HOST",
                "value": memgraph_bolt_host,
            },
            {
                "name": "MEMGRAPH_PORT",
                "value": memgraph_bolt_port,
            },
            {
                "name": "CYPHER_PROVIDER",
                "value": cypher_provider,
            },
            {
                "name": "CYPHER_MODEL",
                "value": cypher_model,
            },
            {
                "name": "ORCHESTRATOR_PROVIDER",
                "value": orchestrator_provider,
            },
            {
                "name": "ORCHESTRATOR_MODEL",
                "value": orchestrator_model,
            },
            {
                "name": "CYPHER_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": llm_secret_name,
                        "key": "CYPHER_API_KEY",
                        "optional": True,
                    }
                },
            },
            {
                "name": "ORCHESTRATOR_API_KEY",
                "valueFrom": {
                    "secretKeyRef": {
                        "name": llm_secret_name,
                        "key": "ORCHESTRATOR_API_KEY",
                        "optional": True,
                    }
                },
            },
        ],
        "resources": {
            "requests": {"cpu": "200m", "memory": "512Mi"},
            "limits": {"memory": "1Gi"},
        },
    },
    opts=ResourceOptions(
        parent=cgr_namespace,
        depends_on=[cgr_namespace, llm_vault_secret],
        delete_before_replace=True,
    ),
)

export("mcpserver_name", "code-graph-rag")
export("namespace", cgr_namespace_name)
export("llm_secret_name", llm_secret_name)
