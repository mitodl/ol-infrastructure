# Vector Log Proxy Kubernetes Deployment

This directory contains Pulumi code to deploy vector-log-proxy to the operations Kubernetes cluster as a Deployment with Traefik reverse proxy sidecar.

## Quick Start

Deploy to QA:
```bash
cd src/ol_infrastructure/infrastructure/vector_log_proxy/
pulumi stack select infrastructure.vector_log_proxy.operations.QA
pulumi preview
pulumi up
```

Monitor deployment:
```bash
kubectl -n operations get pods -l pulumi_stack=infrastructure.vector_log_proxy.operations.QA
kubectl -n operations logs -f deployment/vector-log-proxy -c vector
```

## Architecture

**Pod Components:**
- **Traefik** sidecar: TLS termination and reverse proxy
  - Ports: 80 (HTTP), 443 (HTTPS), 9000, 9443 (passthrough)
  - Routes HTTPS traffic to Vector containers

- **Vector** container: Log processing and forwarding
  - Ports: 9000 (Heroku logs), 9443 (Fastly logs)
  - Transforms and forwards to Grafana Cloud Loki

**Configuration Sources:**
- ConfigMaps: `vector-log-proxy-config`, `traefik-log-proxy-config`
- K8s Secrets: `vector-log-proxy-credentials` (synced from Vault)  # pragma: allowlist secret
- Vault Secret: `secret-vector-log-proxy/basic_auth_credentials`  # pragma: allowlist secret

## Key Files

- `__main__.py`: Pulumi deployment code
- `vector_log_proxy_policy.hcl`: Vault policy for K8s auth
- `Pulumi.infrastructure.vector_log_proxy.operations.{QA,Production,CI}.yaml`: Stack configs

## Environment Variables

Configured via Vault K8s secrets sync:
```
GRAFANA_CLOUD_API_KEY
GRAFANA_CLOUD_PROMETHEUS_API_USER
GRAFANA_CLOUD_LOKI_API_USER
HEROKU_PROXY_USERNAME
HEROKU_PROXY_PASSWORD
FASTLY_PROXY_USERNAME
FASTLY_PROXY_PASSWORD
```

## External Endpoints

- Heroku: `https://log-proxy[-qa].odl.mit.edu/heroku` (HTTPS + basic auth)
- Fastly: `https://log-proxy[-qa].odl.mit.edu/fastly` (HTTPS + basic auth)

**Note:** Path-based routing via Traefik gateway on standard HTTPS port 443.

## Troubleshooting

**Check pod status:**
```bash
kubectl -n operations describe pod <pod-name>
```

**View container logs:**
```bash
# Vector
kubectl -n operations logs deployment/vector-log-proxy -c vector

# Traefik
kubectl -n operations logs deployment/vector-log-proxy -c traefik
```

**Verify secrets:**
```bash
kubectl -n operations get secret vector-log-proxy-credentials -o yaml
```

**Test connectivity:**
```bash
kubectl -n operations run -it --rm debug --image=curlimages/curl --restart=Never -- \
  curl -v https://vector-log-proxy:443/ping -k
```

## Migration from EC2

This deployment replaces the previous EC2 Auto Scaling Group setup. Key changes:
- No AMI build needed (uses official Vector Docker image)
- ConfigMaps replace cloud-config user-data
- Vault Secrets Operator replaces Vault Agent
- K8s Service LoadBalancer replaces AWS ALB
- Pod anti-affinity replaces ASG instance distribution

See `docs/adr/0006-migrate-vector-log-proxy-to-kubernetes.md` for detailed migration rationale.
