# Apache APISIX Standalone Mode Setup

**Last Updated:** October 29, 2025 (17:28 UTC)
**Configuration File:** `apisix_official.py`
**Chart Version:** 2.12.2 (APISIX 3.14.1)
**Deployment Status:** ✅ Fully Operational

## Overview

This deployment configures Apache APISIX as an ingress controller in **standalone mode** with YAML-based configuration provider and the `apisix-standalone` ingress controller. This architecture eliminates the need for etcd while maintaining dynamic configuration capabilities through Kubernetes CRDs.

## Architecture

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Kubernetes Cluster (operations namespace)                              │
│                                                                         │
│  ┌──────────────────────────┐                                          │
│  │  Application Namespaces  │                                          │
│  │  ┌────────────────────┐  │                                          │
│  │  │  ApisixRoute       │  │        Watches All Namespaces            │
│  │  │  ApisixPluginConfig│  │◄─────────────┐                          │
│  │  │  ApisixUpstream    │  │              │                           │
│  │  └────────────────────┘  │              │                           │
│  └──────────────────────────┘              │                           │
│                                             │                           │
│  ┌──────────────────────────────────────┐  │                           │
│  │  Operations Namespace                │  │                           │
│  │                                      │  │                           │
│  │  ┌────────────────────────────────┐ │  │                           │
│  │  │  Ingress Controller Pod(s)     │ │  │                           │
│  │  │  - ServiceAccount with RBAC ───┼─┼──┘                           │
│  │  │  - Watches CRDs cluster-wide   │ │                              │
│  │  │  - Validates via webhook       │ │                              │
│  │  │  - Reads GatewayProxy config   │ │                              │
│  │  └──────────────┬─────────────────┘ │                              │
│  │                 │                    │                              │
│  │                 │ Connects via service:  │                              │
│  │                 │ apache-apisix-admin    │                              │
│  │                 │ (port 9180)            │                              │
│  │                 │                    │                              │
│  │  ┌──────────────▼─────────────────┐ │                              │
│  │  │  APISIX Gateway Pod(s)         │ │                              │
│  │  │                                │ │                              │
│  │  │  ┌──────────────────────────┐  │ │                              │
│  │  │  │ Admin API :9180          │  │ │                              │
│  │  │  │ (apache-apisix-admin svc)│◄─┼─┼──── Ingress Controller      │
│  │  │  │ - AdminKey auth required │  │ │      (with FQDN)             │
│  │  │  └──────────────────────────┘  │ │                              │
│  │  │                                │ │                              │
│  │  │  ┌──────────────────────────┐  │ │                              │
│  │  │  │ Gateway :9080 (HTTP)     │  │ │                              │
│  │  │  │ Gateway :9443 (HTTPS)    │◄─┼─┼──── LoadBalancer Service    │
│  │  │  │ (apache-apisix-gateway)  │  │ │      (AWS NLB)               │
│  │  │  │ Type: LoadBalancer       │  │ │      - Type: network         │
│  │  │  │ External-IP: NLB DNS     │  │ │      - Scheme: internet-facing│
│  │  │  │                          │  │ │      - Target Type: IP       │
│  │  │  └──────────────────────────┘  │ │                              │
│  │  │                                │ │                              │
│  │  │  ┌──────────────────────────┐  │ │                              │
│  │  │  │ apisix.yaml (ConfigMap)  │  │ │                              │
│  │  │  │ - Initial empty config   │  │ │                              │
│  │  │  │ - Mounted at startup     │  │ │                              │
│  │  │  └──────────────────────────┘  │ │                              │
│  │  └────────────────────────────────┘ │                              │
│  │                                      │                              │
│  │  ┌────────────────────────────────┐ │                              │
│  │  │  GatewayProxy CRD              │ │                              │
│  │  │  (apache-apisix-config)        │ │                              │
│  │  │  - Admin API endpoint config   ├─┼──── Read by Controller      │
│  │  │  - Authentication credentials  │ │                              │
│  │  └────────────────────────────────┘ │                              │
│  └──────────────────────────────────────┘                              │
│                                                                         │
│  ┌──────────────────────────────────────┐                              │
│  │  IngressClass                        │                              │
│  │  (apache-apisix)                     │                              │
│  │  - For standard Ingress resources    │                              │
│  │  - References GatewayProxy           │                              │
│  └──────────────────────────────────────┘                              │
└─────────────────────────────────────────────────────────────────────────┘
                           │
                           │ Routes traffic based
                           │ on ApisixRoute configs
                           ▼
                    ┌──────────────┐
                    │  Backend     │
                    │  Services    │
                    └──────────────┘
```

## Deployment Mode Details

### APISIX Gateway Configuration

**Mode:** `traditional`
**Role:** `traditional`
**Config Provider:** `yaml`

This configuration:
- Loads an initial empty `apisix.yaml` file from a ConfigMap at startup
- Enables Admin API on port 9180 (ClusterIP service)
- Accepts configuration updates via the Admin API
- Stores configuration in-memory (stateless, no persistence)

**Important:** The initial "worker has not received configuration" message in `/status/ready` is **expected behavior** during startup. Workers receive configuration once the ingress controller connects and pushes CRD-defined routes.

### Ingress Controller Configuration

**Provider Type:** `apisix-standalone`
**Controller Name:** `apisix.apache.org/apache-apisix-ingress-controller`
**IngressClass:** `apache-apisix`

This configuration:
- Watches all namespaces for APISIX CRDs
- Validates resources via admission webhook
- Connects to APISIX Admin API using GatewayProxy config
- Translates Kubernetes CRDs into APISIX configuration
- Pushes configuration via Admin API

### GatewayProxy CRD

**Name:** `apache-apisix-config`
**Namespace:** `operations`
**API Version:** `apisix.apache.org/v1alpha1`

Configuration tells the ingress controller:
- **Admin API service:** `apache-apisix-admin` (port 9180)
- **Authentication:** AdminKey (from Vault secret)
- **Provider type:** ControlPlane

This is a **cluster-scoped configuration resource** that does not affect routing. It only configures HOW the controller connects to APISIX.

**Critical Update (2025-10-29):** The configuration now uses the `service` format instead of `endpoints` array. Since both the GatewayProxy and Admin API service are in the same namespace (`operations`), Kubernetes service discovery resolves the service name correctly without requiring a FQDN.

## Configuration Files

### 1. Initial apisix.yaml ConfigMap

**Name:** `apache-apisix-standalone-config`
**Purpose:** Provides required YAML structure for APISIX to start

```yaml
routes: []
upstreams: []
services: []
consumers: []
plugin_metadata: []
global_rules: []
```

Mounted at: `/usr/local/apisix/conf/apisix.yaml`

This file is intentionally empty - the ingress controller populates configuration dynamically via the Admin API.

### 2. Helm Chart Values

Key values configured in `apisix_official.py`:

```python
{
    "apisix": {
        "deployment": {
            "mode": "traditional",
            "role": "traditional",
            "role_traditional": {
                "config_provider": "yaml",
            },
        },
        "admin": {
            "enabled": True,
            "port": 9180,
            "credentials": {
                "admin": "<from-vault>",
                "viewer": "<from-vault>",
            },
        },
    },
    "etcd": {
        "enabled": False,  # No etcd required
    },
    "ingress-controller": {
        "enabled": True,
        "rbac": {
            "create": True,  # Critical: Creates ClusterRole with CRD permissions
        },
        "serviceAccount": {
            "create": True,
            "name": "apache-apisix-ingress-controller",
        },
        "config": {
            "provider": {
                "type": "apisix-standalone",
            },
            "kubernetes": {
                "ingressClass": "apache-apisix",
            },
        },
        "gatewayProxy": {
            "createDefault": True,
            "provider": {
                "type": "ControlPlane",
                "controlPlane": {
                    # Use service reference (same namespace)
                    "service": {
                        "name": "apache-apisix-admin",
                        "port": 9180,
                    },
                    "auth": {
                        "type": "AdminKey",
                        "adminKey": {
                            "value": "<from-vault>",
                        },
                    },
                },
            },
        },
    },
}
```

## LoadBalancer Service Configuration

### AWS Network Load Balancer (NLB)

The gateway service is configured as type `LoadBalancer` to provision an AWS NLB:

```python
{
    "service": {  # Changed from "gateway" to "service" (correct helm chart key)
        "type": "LoadBalancer",
        "annotations": {
            "external-dns.alpha.kubernetes.io/hostname": "<comma-separated-domains>",
            "service.beta.kubernetes.io/aws-load-balancer-name": "applications-ci-apisix",
            "service.beta.kubernetes.io/aws-load-balancer-type": "external",
            "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
            "service.beta.kubernetes.io/aws-load-balancer-scheme": "internet-facing",
            "service.beta.kubernetes.io/aws-load-balancer-cross-zone-load-balancing-enabled": "true",
            "service.beta.kubernetes.io/aws-load-balancer-subnets": "<public-subnet-ids>",
            "service.beta.kubernetes.io/aws-load-balancer-additional-resource-tags": "<tags>",
        },
        "http": {
            "enabled": True,
            "servicePort": 80,
            "containerPort": 9080,
        },
        "tls": {
            "enabled": True,
            "servicePort": 443,
            "containerPort": 9443,
        },
    },
}
```

## Critical Configuration Requirements

### 1. RBAC Permissions (Required)

The ingress controller **must** have RBAC enabled to function:

```python
"rbac": {
    "create": True,
}
```

This creates:
- **ClusterRole** (`apache-apisix-apisix-ingress-manager-role`) with permissions to:
  - Watch and list `ApisixRoute`, `ApisixPluginConfig`, `ApisixUpstream` CRDs cluster-wide
  - Read `Services`, `Endpoints`, `ConfigMaps`, `Secrets` in all namespaces
  - Manage `IngressClass` resources
  - Update status subresources on APISIX CRDs
  - Create/update `Events` for monitoring
- **ClusterRoleBinding** binding the role to the ServiceAccount
- **ServiceAccount** for pod authentication

**Without RBAC:** The controller cannot watch CRDs, read service information, or access secrets, resulting in no configuration being pushed to APISIX.

### 2. Service Configuration for GatewayProxy (Required)

The GatewayProxy must use the `service` reference format:

```python
"service": {
    "name": "apache-apisix-admin",
    "port": 9180,
}
```

**Why this works:**
- The GatewayProxy CRD is in the `operations` namespace
- The Admin API service is also in `operations` namespace
- Kubernetes DNS resolves same-namespace services automatically
- Service name alone is sufficient for resolution

**Updated 2025-10-29:** Previous documentation incorrectly required FQDN format with `endpoints` array. Testing confirmed that `service` reference works correctly when both resources are in the same namespace.

### 3. AdminKey Authentication (Required)

The Admin API requires authentication via AdminKey:

```python
"auth": {
    "type": "AdminKey",
    "adminKey": {
        "value": "<admin-key-from-vault>",
    },
}
```

This must match the `admin` credential configured in the APISIX deployment.

## Resource Naming Convention

All resources use the release name `apache-apisix` as a prefix:

| Resource Type | Name | Note |
|---------------|------|------|
| Gateway LoadBalancer Service | `apache-apisix-gateway` | AWS NLB provisioned |
| Admin API ClusterIP Service | `apache-apisix-admin` | Internal only |
| Ingress Controller Deployment | `apache-apisix-ingress-controller` | 3 replicas |
| ConfigMap (initial config) | `apache-apisix-standalone-config` | Empty apisix.yaml |
| GatewayProxy CRD | `apache-apisix-config` | Admin API config |
| IngressClass | `apache-apisix` | Required for routes |
| Service Account (Controller) | `apache-apisix-ingress-controller` | With RBAC |
| Service Account (APISIX) | `default` | No custom SA needed |

## Creating Routes

Your routes **must include** the `ingressClassName` field to be managed by this controller.

### Example: ApisixRoute

```python
from ol_infrastructure.components.services.k8s import (
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixPluginConfig,
)

route = OLApisixRoute(
    name="my-application-route",
    k8s_namespace="my-app-namespace",  # Can be any namespace
    k8s_labels={"app": "my-app"},
    ingress_class_name="apache-apisix",  # ← REQUIRED!
    route_configs=[
        OLApisixRouteConfig(
            route_name="api-route",
            priority=10,
            hosts=["api.example.com"],
            paths=["/api/*"],
            backend_service_name="my-service",
            backend_service_port=8080,
            plugins=[
                OLApisixPluginConfig(
                    name="cors",
                    config={"allow_origins": "*"},
                ),
            ],
        ),
    ],
)
```

**Critical:** The `ingressClassName: apache-apisix` field is required for the controller to discover and manage the route.

### Example: ApisixPluginConfig (Shared Plugins)

```python
from ol_infrastructure.components.services.k8s import (
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)

shared_plugins = OLApisixSharedPlugins(
    name="common-security-plugins",
    plugin_config=OLApisixSharedPluginsConfig(
        application_name="my-app",
        k8s_namespace="my-app-namespace",
        plugins=[
            {"name": "ip-restriction", "config": {"whitelist": ["10.0.0.0/8"]}},
            {"name": "limit-req", "config": {"rate": 100, "burst": 50}},
        ],
    ),
)

# Reference in route:
route_config = OLApisixRouteConfig(
    route_name="protected-route",
    shared_plugin_config_name="my-app-common-security-plugins",
    # ... other config
)
```

### Example: ApisixUpstream (External Service)

```python
from ol_infrastructure.components.services.k8s import (
    OLApisixExternalUpstream,
    OLApisixExternalUpstreamConfig,
)

external_upstream = OLApisixExternalUpstream(
    name="legacy-api-upstream",
    external_upstream_config=OLApisixExternalUpstreamConfig(
        application_name="my-app",
        k8s_namespace="my-app-namespace",
        external_hostname="legacy-api.example.com",
        scheme="https",
    ),
)

# Reference in route:
route_config = OLApisixRouteConfig(
    route_name="proxy-to-legacy",
    upstream="my-app-legacy-api-upstream",  # No backend service needed
    # ... other config
)
```

## How Configuration Sync Works

1. **You apply a CRD** (e.g., `kubectl apply -f apisix-route.yaml`)
2. **Webhook validates** the resource schema
3. **Ingress controller detects** the new/updated CRD
4. **Controller translates** the CRD spec into APISIX configuration
5. **Controller pushes** configuration via Admin API to `apache-apisix-admin:9180`
6. **APISIX applies** the configuration in-memory
7. **Traffic routes** according to the new configuration

**Sync Period:** 1 minute (configurable via `config.provider.syncPeriod`)

## AWS Network Load Balancer

### Deployment Status

The gateway service creates an AWS Network Load Balancer (NLB) with the following configuration:

**Service Configuration:**
- **Type:** LoadBalancer (changed from NodePort)
- **Service Name:** `apache-apisix-gateway`
- **External DNS:** Automatic via external-dns annotation
- **Ports:** 80 (HTTP → 9080), 443 (HTTPS → 9443)

**NLB Configuration:**
- **Name:** `applications-ci-apisix` (or `<cluster-name>-apisix`)
- **Type:** `network` (Network Load Balancer)
- **Scheme:** `internet-facing`
- **Target Type:** `ip` (pod-level routing)
- **Cross-Zone Load Balancing:** Enabled
- **Subnets:** Public subnets from VPC configuration

**Target Groups:**
- **HTTP Target Group:** TCP port 9080, IP targets to APISIX pods
- **HTTPS Target Group:** TCP port 9443, IP targets to APISIX pods
- **Health Checks:** TCP-based (no HTTP health check configured)

### Verification Commands

```bash
# Check service has external IP
kubectl get svc apache-apisix-gateway -n operations

# Get NLB DNS name
kubectl get svc apache-apisix-gateway -n operations \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'

# Check AWS NLB details
aws elbv2 describe-load-balancers --names applications-ci-apisix

# Verify target groups
aws elbv2 describe-target-groups \
  --load-balancer-arn $(aws elbv2 describe-load-balancers \
    --names applications-ci-apisix \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# Check target health
aws elbv2 describe-target-health \
  --target-group-arn <target-group-arn>
```

### DNS Configuration

The external-dns controller automatically creates DNS records for all domains listed in the service annotation:

```yaml
external-dns.alpha.kubernetes.io/hostname: "domain1.com,domain2.com,..."
```

**Expected Domains (CI environment):**
- `api-pay-ci.ol.mit.edu`
- `api-learn-ai-ci.ol.mit.edu`
- `api.ci.learn.mit.edu`
- `nb.ci.learn.mit.edu`
- `ci.mitxonline.mit.edu`
- `api.ci.mitxonline.mit.edu`
- `studio-backend.ci.learn.mit.edu`
- `courses-backend.ci.learn.mit.edu`
- `preview.courses.ci.learn.mit.edu`

All domains will resolve to the NLB's IP addresses.

## Deployment Workflow

### Initial Deployment

```bash
# 1. Deploy infrastructure (creates ConfigMap, then Helm release)
pulumi up

# 2. Wait for pods to be ready
kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=apisix \
  -n operations --timeout=300s

kubectl wait --for=condition=ready pod \
  -l app.kubernetes.io/name=ingress-controller \
  -n operations --timeout=300s

# 3. Verify GatewayProxy config is loaded
kubectl logs -n operations \
  -l app.kubernetes.io/name=ingress-controller \
  | grep "GatewayProxy"
# Should see: "Successfully loaded GatewayProxy config"

# 4. Check APISIX received configuration
kubectl logs -n operations \
  -l app.kubernetes.io/name=apisix \
  | grep -E "configuration|worker"
```

### Updating Routes

Simply update your Pulumi code with new `OLApisixRoute` definitions and run:

```bash
pulumi up
```

The ingress controller will detect changes within 1 minute and sync to APISIX.

### Manual Verification

```bash
# List all ApisixRoute resources
kubectl get apisixroute -A

# Describe a specific route
kubectl describe apisixroute <name> -n <namespace>

# Check APISIX routes via Admin API (from within cluster)
kubectl exec -n operations deployment/apache-apisix -- \
  curl -s http://localhost:9180/apisix/admin/routes \
  -H "X-API-KEY: $(kubectl get secret -n operations <secret-name> -o jsonpath='{.data.admin_key}' | base64 -d)"
```

## Key Benefits

✅ **No etcd dependency** - Simpler operations, fewer moving parts
✅ **Kubernetes-native** - Configuration via CRDs, GitOps-friendly
✅ **Stateless** - APISIX pods can restart without losing config (re-synced from CRDs)
✅ **Dynamic updates** - No pod restarts needed for route changes
✅ **Multi-tenancy** - Routes in any namespace are supported
✅ **Admission webhook** - Validates CRDs before applying
✅ **Backward compatible** - No changes to existing route definitions

## Important Considerations

### Configuration Persistence

⚠️ **Configuration is in-memory only.** APISIX pod restarts will clear configuration until the ingress controller re-syncs CRDs (happens automatically within 1 minute).

### Admin API Security

- Admin API is **ClusterIP only** (not exposed externally)
- Requires Admin Key authentication
- IP allowlist set to `0.0.0.0/0` (all internal cluster IPs)
- Consider restricting to specific pod CIDRs if security requirements demand it

### Resource Limits

Current configuration:
- **APISIX pods:** 100m-500m CPU, 200Mi-400Mi memory, 3-5 replicas
- **Ingress controller:** 50m CPU, 50Mi-256Mi memory, 2 replicas

Adjust based on traffic patterns and number of routes.

### Blue-Green Migration

This deployment supports separate domains via `apisix_official_domains` config key, allowing gradual migration from the Bitnami chart deployment.

## Configuration Validation Checklist

Before deploying or when troubleshooting, verify these critical settings:

### ✅ RBAC Configuration
```python
"ingress-controller": {
    "rbac": {"create": True},  # Must be True
    "serviceAccount": {
        "create": True,
        "name": "apache-apisix-ingress-controller",
    },
}
```

### ✅ GatewayProxy Service Reference (Corrected)
```python
"gatewayProxy": {
    "provider": {
        "controlPlane": {
            "service": {
                "name": "apache-apisix-admin",
                "port": 9180,
            },
            # NOT endpoints array - that was incorrect
        }
    }
}
```

### ✅ Admin API Authentication
```python
"auth": {
    "type": "AdminKey",
    "adminKey": {
        "value": "<must-match-apisix-admin-credential>",
    },
}
```

### ✅ APISIX Configuration
```python
"apisix": {
    "deployment": {
        "role": "traditional",
        "role_traditional": {
            "config_provider": "yaml",  # Required for standalone mode
        },
    },
    "admin": {
        "enabled": True,  # Must be enabled
        "port": 9180,
    },
}
```

### ✅ Initial ConfigMap Mounted
```python
"extraVolumes": [
    {
        "name": "apisix-config",
        "configMap": {"name": "apache-apisix-standalone-config"},
    },
],
"extraVolumeMounts": [
    {
        "name": "apisix-config",
        "mountPath": "/usr/local/apisix/conf/apisix.yaml",
        "subPath": "apisix.yaml",
    },
],
```

### ✅ Service Type (LoadBalancer)
```python
"service": {  # Correct key (not "gateway")
    "type": "LoadBalancer",
    "annotations": {
        "service.beta.kubernetes.io/aws-load-balancer-type": "external",
        "service.beta.kubernetes.io/aws-load-balancer-nlb-target-type": "ip",
        "external-dns.alpha.kubernetes.io/hostname": "<domains>",
        # ... other AWS annotations
    },
}
```

### ✅ IngressClassName in Routes (Required)
```yaml
apiVersion: apisix.apache.org/v2
kind: ApisixRoute
metadata:
  name: my-route
spec:
  ingressClassName: apache-apisix  # ← Required!
  http:
    - name: route-rule
      # ...
```

### Verification Commands

```bash
# 1. Verify all required resources exist
kubectl get gatewayproxy apache-apisix-config -n operations
kubectl get clusterrole apache-apisix-apisix-ingress-manager-role
kubectl get serviceaccount -n operations apache-apisix-ingress-controller
kubectl get configmap -n operations apache-apisix-standalone-config
kubectl get ingressclass apache-apisix

# 2. Verify LoadBalancer service has external IP
kubectl get svc apache-apisix-gateway -n operations
# Should show: TYPE=LoadBalancer, EXTERNAL-IP=<nlb-dns-name>

# 3. Check NLB in AWS
aws elbv2 describe-load-balancers --names applications-ci-apisix

# 4. Verify controller can connect to Admin API
kubectl logs -n operations -l app.kubernetes.io/name=ingress-controller | grep -i "admin\|gateway\|connect"

# 5. Verify routes have ingressClassName
kubectl get apisixroute -A -o json | jq -r '.items[] | select(.spec.ingressClassName == "apache-apisix") | "\(.metadata.namespace)/\(.metadata.name)"'

# 6. Check for RBAC errors
kubectl logs -n operations -l app.kubernetes.io/name=ingress-controller | grep -i "forbidden\|unauthorized"
```

## Troubleshooting

### Issue: "worker has not received configuration"

**Status:** ✅ Expected during initial startup
**Explanation:** APISIX workers wait for configuration from the controller. This message appears until:
1. Ingress controller starts and reads GatewayProxy config
2. Controller connects to Admin API
3. Controller discovers and syncs CRD resources
4. Configuration is pushed to APISIX

**Resolution:** Wait 30-60 seconds after controller starts. If persists, check:
- Ingress controller logs for connection errors
- GatewayProxy configuration (see below)
- RBAC permissions are created

### Issue: "no GatewayProxy configs provided"

**Cause:** Ingress controller can't find or read GatewayProxy CRD

**Resolution Steps:**

1. **Verify GatewayProxy exists:**
```bash
kubectl get gatewayproxy apache-apisix-config -n operations -o yaml
```

2. **Check endpoint configuration:**
```bash
kubectl get gatewayproxy apache-apisix-config -n operations -o jsonpath='{.spec.provider.controlPlane.endpoints}'
```
Should show: `["http://apache-apisix-admin.operations.svc.cluster.local:9180"]`

3. **Verify RBAC permissions:**
```bash
kubectl get clusterrole apache-apisix-apisix-ingress-manager-role
kubectl get clusterrolebinding apache-apisix-apisix-ingress-manager-rolebinding
```

### Issue: Controller can't connect to Admin API

**Symptoms:** Logs show connection refused, timeout, or DNS resolution errors

**Common Causes & Solutions:**

1. **Using service name without namespace:**
   - ❌ Bad: `service: { name: "apache-apisix-admin" }`
   - ✅ Good: `endpoints: ["http://apache-apisix-admin.operations.svc.cluster.local:9180"]`

2. **Wrong namespace in FQDN:**
   - Verify service exists: `kubectl get svc apache-apisix-admin -n operations`
   - Check FQDN format: `<service>.<namespace>.svc.cluster.local:<port>`

3. **Authentication failure:**
   - Verify admin key matches: Compare secret value with APISIX config
   - Check key format: Should be raw key value, not base64 encoded

4. **Network policies blocking traffic:**
   - Verify ingress controller pods can reach APISIX pods
   - Check for NetworkPolicy restrictions

### Issue: RBAC permission errors

**Symptoms:** Controller logs show "forbidden" errors when watching resources

**Resolution:**

```bash
# Verify RBAC resources exist
kubectl get clusterrole | grep apache-apisix
kubectl get clusterrolebinding | grep apache-apisix
kubectl get serviceaccount -n operations apache-apisix-ingress-controller

# Check what permissions the role has
kubectl describe clusterrole apache-apisix-apisix-ingress-manager-role

# Verify binding is correct
kubectl describe clusterrolebinding apache-apisix-apisix-ingress-manager-rolebinding
```

**Required permissions:**
- `apiGroups: ["apisix.apache.org"]` - All APISIX CRDs
- `apiGroups: [""]` - Services, Endpoints, Secrets, ConfigMaps
- `apiGroups: ["networking.k8s.io"]` - IngressClass
- Verbs: `get`, `list`, `watch`, `update` (for status)

### Issue: Routes not appearing in APISIX

**Common Cause:** Missing `ingressClassName` field in ApisixRoute CRD

**Diagnostics:**

```bash
# 1. Check if route has ingressClassName
kubectl get apisixroute <name> -n <namespace> -o jsonpath='{.spec.ingressClassName}'
# Should output: apache-apisix

# 2. List all routes with correct ingressClassName
kubectl get apisixroute -A -o json | jq -r '.items[] | select(.spec.ingressClassName == "apache-apisix") | "\(.metadata.namespace)/\(.metadata.name)"'

# 3. Check route status
kubectl get apisixroute <name> -n <namespace> -o jsonpath='{.status.conditions[0]}'
# Should show: "status":"True","type":"Accepted"

# 4. Check controller logs
kubectl logs -n operations -l app.kubernetes.io/name=ingress-controller --tail=100 | grep <route-name>
```

**Resolution:** Add `spec.ingressClassName: apache-apisix` to all ApisixRoute CRDs.

### Issue: LoadBalancer service stuck in pending

**Cause:** AWS Load Balancer Controller not creating NLB

**Diagnostics:**

```bash
# Check service events
kubectl describe svc apache-apisix-gateway -n operations

# Verify AWS Load Balancer Controller is running
kubectl get pods -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller

# Check controller logs
kubectl logs -n kube-system -l app.kubernetes.io/name=aws-load-balancer-controller
```

**Resolution:** Ensure AWS Load Balancer Controller is installed and has proper IAM permissions.

### Issue: Gateway returns 404 for configured route

**Diagnostics:**

```bash
# 1. Verify route exists in APISIX
kubectl exec -n operations deployment/apache-apisix -- \
  curl -s http://localhost:9180/apisix/admin/routes \
  -H "X-API-KEY: <admin-key>" | jq

# 2. Check route priority and host matching
# 3. Verify backend service exists and is ready
kubectl get svc <backend-service> -n <namespace>
```

## Version Information

- **APISIX Helm Chart:** 2.12.x
- **APISIX Version:** 3.14.x
- **Ingress Controller:** 2.0.0-rc5 (bundled with chart)
- **GatewayProxy API:** v1alpha1

## Configuration Flow Summary

Understanding the complete flow from deployment to working routes:

### 1. Initial Deployment
```
Pulumi creates resources in order:
  1. ConfigMap (apache-apisix-standalone-config) with empty apisix.yaml
  2. Helm Release deploys:
     - APISIX gateway pods (mount ConfigMap, enable Admin API)
     - Ingress controller pods (with RBAC ServiceAccount)
     - GatewayProxy CRD (with FQDN endpoint config)
     - IngressClass (references GatewayProxy)
```

### 2. Startup Sequence
```
APISIX Gateway Startup:
  ├─ Loads apisix.yaml from ConfigMap ✅
  ├─ Starts Admin API on :9180 ✅
  ├─ Workers wait for configuration ⏳
  └─ Shows "worker has not received configuration" (expected)

Ingress Controller Startup:
  ├─ Reads GatewayProxy CRD ✅
  ├─ Resolves FQDN: apache-apisix-admin.operations.svc.cluster.local:9180 ✅
  ├─ Connects to Admin API with AdminKey auth ✅
  ├─ Watches for ApisixRoute/PluginConfig/Upstream CRDs (via RBAC) ✅
  └─ Ready to sync configuration
```

### 3. Configuration Sync
```
When ApisixRoute CRD is created/updated:
  1. Admission webhook validates CRD schema ✅
  2. Ingress controller detects change (via watch) ✅
  3. Controller translates CRD to APISIX config ✅
  4. Controller POSTs to Admin API: /apisix/admin/routes ✅
  5. APISIX stores config in-memory ✅
  6. Workers receive configuration ✅
  7. Route becomes active ✅
```

### 4. Traffic Flow
```
External Request
  │
  ├─► LoadBalancer (apache-apisix-gateway)
  │     │
  │     └─► APISIX Gateway Pod
  │           ├─ Matches route by host/path
  │           ├─ Applies plugins (auth, rate-limit, etc.)
  │           └─► Backend Service (from route config)
  │                 │
  │                 └─► Application Pods
  │
  └─► Response follows reverse path
```

### Key Integration Points

| Component | Connects To | Via | Purpose |
|-----------|-------------|-----|---------|
| Ingress Controller | GatewayProxy CRD | Read | Get Admin API endpoint |
| Ingress Controller | APISIX Admin API | HTTP (FQDN) | Push configuration |
| Ingress Controller | Kubernetes API | RBAC | Watch CRDs |
| APISIX Gateway | ConfigMap | Volume mount | Initial config file |
| APISIX Gateway | Admin API | Internal :9180 | Receive config updates |
| External Traffic | APISIX Gateway | LoadBalancer | Route requests |
| APISIX Gateway | Backend Services | ClusterIP | Proxy to upstreams |

## Related Documentation

- [APISIX Deployment Modes](https://apisix.apache.org/docs/apisix/deployment-modes/)
- [Ingress Controller Concepts](https://apisix.apache.org/docs/ingress-controller/concepts/)
- [ApisixRoute CRD Reference](https://apisix.apache.org/docs/ingress-controller/references/apisix_route_v2/)
- [Standalone Mode Guide](https://apisix.apache.org/docs/apisix/standalone/)

## Current Deployment Status (2025-10-29)

### ✅ Operational Components

| Component | Status | Details |
|-----------|--------|---------|
| **Helm Release** | ✅ Deployed | Version 2.12.2, Revision 7 |
| **APISIX Pods** | ✅ Running | 3/3 ready, autoscaling 3-5 |
| **Ingress Controller** | ✅ Running | 3/3 pods, watching CRDs |
| **AWS NLB** | ✅ Provisioned | `applications-ci-apisix-<hash>.elb.us-east-1.amazonaws.com` |
| **Target Groups** | ✅ Healthy | IP targets for ports 9080, 9443 |
| **DNS Records** | ✅ Active | 9 domains via external-dns |
| **GatewayProxy** | ✅ Configured | Service reference to Admin API |
| **IngressClass** | ✅ Active | `apache-apisix` |
| **RBAC** | ✅ Enabled | ClusterRole with CRD permissions |

### 📊 Managed Routes

**Current:** 2 ApisixRoute resources with `ingressClassName: apache-apisix`
- `mitlearn/ol-mitlearn-k8s-apisix-route-ci` (Status: Accepted)
- `mitlearn/ol-mitlearn-k8s-apisix-route-no-prefix-ci` (Status: Accepted)

**Pending Migration:** 7 routes need `ingressClassName` field added
- Routes in namespaces: ecommerce, jupyter, learn-ai, mitxonline, mitxonline-openedx

### 🔄 Configuration Flow

```
Kubernetes CRDs → Ingress Controller → Admin API → APISIX Gateway → AWS NLB → Internet
     (GitOps)         (Watching)       (HTTP/9180)    (In-memory)    (IP targets)
```

### 🎯 Key Achievements

- ✅ Eliminated etcd dependency
- ✅ AWS NLB with IP target type (pod-level routing)
- ✅ Automatic DNS management via external-dns
- ✅ CRD-based configuration (GitOps compatible)
- ✅ RBAC-secured ingress controller
- ✅ Same-namespace service resolution for Admin API
- ✅ Proper LoadBalancer service configuration

## Support

For issues specific to this deployment:
1. Check ingress controller logs: `kubectl logs -n operations -l app.kubernetes.io/name=ingress-controller`
2. Check APISIX logs: `kubectl logs -n operations -l app.kubernetes.io/name=apisix`
3. Verify GatewayProxy configuration uses FQDN endpoint
4. Ensure RBAC resources exist and are bound correctly
5. Verify admin key secret exists and matches between components

For upstream issues, consult the [Apache APISIX GitHub repository](https://github.com/apache/apisix).
