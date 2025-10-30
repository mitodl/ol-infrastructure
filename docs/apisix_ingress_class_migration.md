# APISix IngressClass Migration Guide

## Overview

This document explains the changes made to support the Apache APISix helm chart migration from Bitnami, specifically around the `ingressClassName` field requirement.

## Background

The Apache APISix helm chart operates in **standalone mode with GatewayProxy** provider. In this configuration:
- The ingress controller processes both legacy APISix CRDs (ApisixRoute, ApisixTls) and Gateway API resources
- **REQUIRES** `spec.ingressClassName` field on all resources to determine which resources to process
- Without `ingressClassName`, resources are ignored (even though status may show "Sync Successfully")

The Bitnami chart did not require this field, causing TLS failures after migration.

## Changes Made

### 1. Component Updates

#### `OLCertManagerCertConfig` (cert_manager.py)
Added configurable ingress class name:
```python
apisixtls_ingress_class: str = "apisix"  # Default for gradual migration
```

This field is used when `create_apisixtls_resource=True` to set the ApisixTls resource's `ingressClassName`.

#### `OLApisixRoute` (k8s.py)
Added configurable ingress class name parameter:
```python
def __init__(
    self,
    name: str,
    route_configs: list[OLApisixRouteConfig],
    k8s_namespace: str,
    k8s_labels: dict[str, str],
    ingress_class_name: str = "apisix",  # Default for backward compatibility
    opts: ResourceOptions | None = None,
):
```

### 2. Default Value Strategy

Both components default to `"apisix"` (not `"apache-apisix"`) to allow controlled migration:

1. **Phase 1 (Current)**: Resources use `ingressClassName: apisix` by default
2. **Phase 2 (Migration)**: Update specific applications to use `ingress_class_name="apache-apisix"`
3. **Phase 3 (Completion)**: Switch default to `"apache-apisix"` and remove overrides

## Migration Path

### Immediate Fix (Already Applied)

All existing ApisixTls resources were manually patched:
```bash
kubectl patch apisixtls <name> -n <namespace> --type=merge \
  -p '{"spec":{"ingressClassName":"apache-apisix"}}'
```

Applied to:
- ✅ mitlearn/mitlearn-cert
- ✅ mitxonline-openedx/edxapp-cert
- ✅ ecommerce/ecommerce-https
- ✅ jupyter/jupyterhub-cert
- ✅ learn-ai/learn-ai-https
- ✅ mitxonline/mitxonline-cert

### Pulumi-Managed Resources

#### Option A: Use Default (Recommended for Now)
No changes needed. Resources will use `ingressClassName: apisix` by default.

```python
# Current usage - no changes needed
cert = OLCertManagerCert(
    name="my-cert",
    cert_config=OLCertManagerCertConfig(
        application_name="myapp",
        k8s_namespace="myns",
        k8s_labels=labels,
        create_apisixtls_resource=True,
        dest_secret_name="my-tls-secret",  # pragma: allowlist-secret
        dns_names=["example.com"],
    ),
)

route = OLApisixRoute(
    name="my-route",
    route_configs=[...],
    k8s_namespace="myns",
    k8s_labels=labels,
)
```

#### Option B: Explicitly Set IngressClass (For Migration)
Override the default to use `apache-apisix`:

```python
cert = OLCertManagerCert(
    name="my-cert",
    cert_config=OLCertManagerCertConfig(
        application_name="myapp",
        k8s_namespace="myns",
        k8s_labels=labels,
        create_apisixtls_resource=True,
        apisixtls_ingress_class="apache-apisix",  # Override
        dest_secret_name="my-tls-secret",  # pragma: allowlist-secret
        dns_names=["example.com"],
    ),
)

route = OLApisixRoute(
    name="my-route",
    route_configs=[...],
    k8s_namespace="myns",
    k8s_labels=labels,
    ingress_class_name="apache-apisix",  # Override
)
```

### Recommended Migration Sequence

1. **Week 1-2**: Test with defaults (`"apisix"`), verify all resources work
2. **Week 3-4**: Migrate critical applications to `"apache-apisix"` explicitly
3. **Week 5-6**: Migrate remaining applications
4. **Week 7**: Change defaults in component code to `"apache-apisix"`
5. **Week 8**: Remove explicit overrides (now redundant with new default)

## IngressClass Configuration

The Apache APISix helm chart creates:

```yaml
apiVersion: networking.k8s.io/v1
kind: IngressClass
metadata:
  name: apache-apisix
spec:
  controller: apisix.apache.org/apisix-ingress-controller
  parameters:
    apiGroup: apisix.apache.org
    kind: GatewayProxy
    name: apache-apisix-config
    namespace: operations
```

Resources must reference this IngressClass name in their `ingressClassName` field.

## Troubleshooting

### TLS Certificate Not Loading

**Symptom**: `failed to match any SSL certificate by SNI: <hostname>`

**Check**:
```bash
# Verify ingressClassName is set
kubectl get apisixtls <name> -n <namespace> -o jsonpath='{.spec.ingressClassName}'

# Should output: apache-apisix (or apisix if using old default)
```

**Fix**:
```bash
kubectl patch apisixtls <name> -n <namespace> --type=merge \
  -p '{"spec":{"ingressClassName":"apache-apisix"}}'
```

### Route Not Working

**Symptom**: 404 errors or routes not being processed

**Check**:
```bash
# Verify ingressClassName is set
kubectl get apisixroute <name> -n <namespace> -o jsonpath='{.spec.ingressClassName}'
```

**Fix**: Update the Pulumi code to pass `ingress_class_name="apache-apisix"`

### Verify SSL Certificates Are Loaded

```bash
# Check SSL version in APISix config (should be > 0)
kubectl run -n operations curl-test --image=curlimages/curl --rm -i --restart=Never -- \
  curl -s http://apache-apisix-admin.operations.svc.cluster.local:9180/apisix/admin/configs \
  -H "X-API-KEY: <admin-key>" | jq '.ssls_conf_version'
```

### Test TLS Connection

```bash
curl -v https://<hostname> 2>&1 | grep -E "SSL|TLS"

# Success shows:
# * SSL connection using TLSv1.3 / TLS_AES_256_GCM_SHA384
```

## References

- [APISix TLS Documentation](https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/)
- [APISix Route Documentation](https://apisix.apache.org/docs/ingress-controller/concepts/apisix_route/)
- [Kubernetes IngressClass](https://kubernetes.io/docs/concepts/services-networking/ingress/#ingress-class)
- [APISix GatewayProxy CRD](https://apisix.apache.org/docs/ingress-controller/references/gateway_proxy/)

## Related Files

- `src/ol_infrastructure/components/services/cert_manager.py` - Certificate and TLS management
- `src/ol_infrastructure/components/services/k8s.py` - APISix route component
- `docs/apisix_tls_fix_summary.md` - Original troubleshooting notes
