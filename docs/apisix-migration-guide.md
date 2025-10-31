# APISix Helm Chart Migration Guide

## Overview

This guide documents the zero-downtime migration from the Bitnami APISix Helm chart to the official Apache APISix chart.

## Architecture Changes

### Current State (Bitnami Chart)
- **Chart**: `oci://registry-1.docker.io/bitnamicharts/apisix` v6.0.0
- **Release Name**: `apisix`
- **Ingress Class**: `apisix`
- **NLB Name**: `{cluster_name}-apisix`
- **Admin Service**: `apisix-control-plane`

### Target State (Official Apache Chart)
- **Chart**: `https://charts.apiseven.com/apisix` v2.12.2
- **Release Name**: `apisix-official`
- **Ingress Class**: `apisix-official`
- **NLB Name**: `{cluster_name}-apisix-official`
- **Admin Service**: `apisix-official-admin`

## Migration Strategy

The migration follows a blue-green deployment approach with four phases:

### Phase 1: Current State (Bitnami Only)
```yaml
# Stack config
eks:apisix_ingress_enabled: "true"
eks:apisix_official_enabled: "false"  # Not set or false
eks:apisix_domains:
  - gateway.example.com
```

**Resources**:
- ✅ Bitnami chart deployed
- ❌ Official chart not deployed
- DNS points to Bitnami NLB

### Phase 2: Blue-Green (Both Charts Running)
```yaml
# Stack config
eks:apisix_ingress_enabled: "true"          # Keep Bitnami running
eks:apisix_official_enabled: "true"         # Deploy official chart
eks:apisix_domains:
  - gateway.example.com                     # Bitnami domains
eks:apisix_official_domains:
  - gateway-v2.example.com                  # Official chart domains (testing)
```

**Resources**:
- ✅ Bitnami chart deployed (production traffic)
- ✅ Official chart deployed (separate NLB/DNS for testing)
- Two NLBs running simultaneously
- Applications can test official chart using `gateway-v2.example.com`

**Testing in Phase 2**:
1. Verify official chart pods are healthy
2. Test APISix routes on new domain
3. Validate metrics and monitoring
4. Smoke test all critical applications

### Phase 3: Traffic Cutover
```yaml
# Stack config - Update DNS to official chart
eks:apisix_ingress_enabled: "true"          # Keep for rollback
eks:apisix_official_enabled: "true"
eks:apisix_domains:
  - gateway.example.com                     # Bitnami (standby)
eks:apisix_official_domains:
  - gateway.example.com                     # Official chart (production)
  - gateway-v2.example.com                  # Official chart (alias)
```

**Actions**:
1. Update `apisix_official_domains` to include production domains
2. Run `pulumi up` - external-dns updates Route53
3. DNS TTL propagation (60s - 5 minutes)
4. Monitor traffic shifting to official chart
5. Both charts remain deployed for quick rollback

**Rollback Procedure** (if issues occur):
```yaml
# Revert DNS quickly
eks:apisix_official_domains:
  - gateway-v2.example.com  # Remove production domain
```

### Phase 4: Cleanup (Remove Bitnami)
```yaml
# Stack config - After confidence period (24-48 hours)
eks:apisix_ingress_enabled: "false"         # Remove Bitnami
eks:apisix_official_enabled: "true"
eks:apisix_official_domains:
  - gateway.example.com
```

**Actions**:
1. Verify no issues for 24-48 hours
2. Set `apisix_ingress_enabled: "false"`
3. Run `pulumi up` - Bitnami chart and NLB removed
4. Update application APISix routes to use `ingressClassName: apisix-official`

## Migration Steps by Environment

### Step 1: Deploy to CI Environment

```bash
cd src/ol_infrastructure/infrastructure/aws/eks/

# Select CI stack
pulumi stack select infrastructure.aws.eks.applications.CI

# Update configuration
pulumi config set eks:apisix_official_enabled true
pulumi config set-all --plaintext \
  eks:apisix_official_domains='["gateway-v2-ci.mitopen.net"]'

# Deploy
pulumi up

# Verify deployment
kubectl -n operations get pods -l app.kubernetes.io/name=apisix
kubectl -n operations get svc apisix-official-gateway
```

### Step 2: Test Official Chart in CI

Test APISix custom resources pointing to official chart:

```yaml
# Example: Update an application's ApisixRoute
apiVersion: apisix.apache.org/v2
kind: ApisixRoute
metadata:
  name: test-route
  namespace: myapp
spec:
  http:
    - name: test
      match:
        hosts:
          - gateway-v2-ci.mitopen.net  # Official chart domain
        paths:
          - /*
      backends:
        - serviceName: myapp
          servicePort: 80
```

Validation checklist:
- [ ] Pods are running and healthy
- [ ] NLB is provisioned and targets are healthy
- [ ] DNS record created for `gateway-v2-ci.mitopen.net`
- [ ] APISix routes are configured via ingress controller
- [ ] HTTP/HTTPS traffic flows correctly
- [ ] Prometheus metrics are exposed
- [ ] Logs are being collected

### Step 3: Cutover CI Traffic

```bash
# Update official chart to use production domain
pulumi config set-all --plaintext \
  eks:apisix_official_domains='["gateway-ci.mitopen.net","gateway-v2-ci.mitopen.net"]'

pulumi up

# Monitor DNS propagation
watch -n 5 'dig gateway-ci.mitopen.net +short'

# Verify traffic shifting
kubectl -n operations logs -l app.kubernetes.io/name=apisix --tail=100
```

### Step 4: Soak Period (24-48 hours)

Monitor for issues:
- Application health checks
- Error rates in logs
- NLB target health
- APISix route configuration
- User-reported issues

### Step 5: Remove Bitnami Chart from CI

```bash
# After successful soak period
pulumi config set eks:apisix_ingress_enabled false

pulumi up

# Verify Bitnami resources removed
kubectl -n operations get pods -l app.kubernetes.io/name=apisix
kubectl -n operations get svc apisix-control-plane  # Should not exist
```

### Step 6: Repeat for QA, Production

Follow same steps for each environment:
1. QA environments
2. data.Production (if applicable)
3. applications.Production (final)

For Production, consider:
- Longer soak period (72+ hours)
- Staged rollout if multiple regions
- Communication plan for users
- Rollback criteria and procedures

## Application Updates Required

After Phase 4 (Bitnami removal), applications must update their APISix custom resources:

### Before (Bitnami Chart)
```yaml
apiVersion: apisix.apache.org/v2
kind: ApisixRoute
metadata:
  annotations:
    # No ingressClassName needed, was default
```

### After (Official Chart)
```yaml
apiVersion: apisix.apache.org/v2
kind: ApisixRoute
metadata:
  annotations:
    kubernetes.io/ingress.class: "apisix-official"  # Explicitly set
```

**Affected Applications**:
- learn_ai
- unified_ecommerce
- mit_learn
- jupyterhub
- dagster
- mitxonline
- keycloak
- edxapp

## Key Configuration Differences

| Feature | Bitnami Chart | Official Chart |
|---------|---------------|----------------|
| **Chart Repo** | `oci://registry-1.docker.io/bitnamicharts/apisix` | `https://charts.apiseven.com` |
| **Chart Version** | 6.0.0 | 2.12.2 |
| **APISix Version** | ~3.11.x | 3.14.1 |
| **Deployment Mode** | `controlPlane.extraConfig.deployment.role` | `apisix.deployment.mode` |
| **Admin API** | `controlPlane.apiTokenAdmin` | `apisix.admin.credentials.admin` |
| **Service Type** | `controlPlane.service.type` | `gateway.type` |
| **Etcd Config** | `etcd.*` | `etcd.*` (similar structure) |
| **Ingress Controller** | `ingressController.extraConfig` | `ingress-controller.config` |
| **Metrics** | `controlPlane.metrics` | `metrics.serviceMonitor` |

## Configuration Files Modified

1. **src/bridge/lib/versions.py**
   - Added `APISIX_OFFICIAL_CHART_VERSION = "2.12.2"`

2. **src/ol_infrastructure/infrastructure/aws/eks/__main__.py**
   - Added import for `setup_apisix_official`
   - Added conditional deployment logic for both charts
   - Added `APISIX_OFFICIAL_CHART` version

3. **src/ol_infrastructure/infrastructure/aws/eks/apisix_official.py** (NEW)
   - Complete setup function for official chart
   - Supports `apisix_official_domains` config

## Troubleshooting

### Issue: Official chart pods not starting
```bash
# Check pod events
kubectl -n operations describe pod -l app.kubernetes.io/name=apisix

# Check etcd connectivity
kubectl -n operations logs -l app.kubernetes.io/name=apisix --tail=100 | grep -i etcd
```

### Issue: DNS not resolving
```bash
# Check external-dns logs
kubectl -n operations logs -l app.kubernetes.io/name=external-dns --tail=50

# Verify Route53 record created
aws route53 list-resource-record-sets --hosted-zone-id <ZONE_ID> | \
  grep -A 5 "gateway-v2"

# Check LoadBalancer service
kubectl -n operations get svc apisix-official-gateway -o yaml
```

### Issue: Routes not being configured
```bash
# Check ingress controller logs
kubectl -n operations logs -l app.kubernetes.io/name=apisix-ingress-controller --tail=100

# Verify admin API connectivity
kubectl -n operations exec -it <apisix-pod> -- curl http://localhost:9180/apisix/admin/routes \
  -H "X-API-KEY: <admin-key>"
```

### Issue: Need to rollback quickly
```bash
# Phase 3 rollback: Revert DNS
pulumi config set-all --plaintext \
  eks:apisix_official_domains='["gateway-v2-ci.mitopen.net"]'
pulumi up

# Phase 2 rollback: Remove official chart entirely
pulumi config set eks:apisix_official_enabled false
pulumi up
```

## Testing Checklist

- [ ] **Infrastructure**
  - [ ] Pods running and healthy
  - [ ] NLB provisioned with healthy targets
  - [ ] DNS records created
  - [ ] External-dns updating records

- [ ] **Functionality**
  - [ ] HTTP traffic works
  - [ ] HTTPS traffic works
  - [ ] APISix routes being configured
  - [ ] Admin API accessible internally
  - [ ] Session cookies working

- [ ] **Observability**
  - [ ] Prometheus metrics exposed
  - [ ] ServiceMonitor created
  - [ ] Logs flowing to aggregation
  - [ ] Custom log format preserved

- [ ] **Performance**
  - [ ] Response times acceptable
  - [ ] No connection errors
  - [ ] HPA scaling working
  - [ ] Resource usage normal

## Success Criteria

Migration is complete when:
1. ✅ All environments running official chart only
2. ✅ All APISix routes functional
3. ✅ No increase in error rates
4. ✅ Monitoring and metrics working
5. ✅ Bitnami chart removed from all environments
6. ✅ Application routes updated to use new ingress class
7. ✅ No DNS-related issues
8. ✅ Documentation updated

## Estimated Timeline

- **CI Environment**: 1 day (deploy + test)
- **CI Soak**: 2 days
- **QA Environments**: 1 day each
- **QA Soak**: 2 days
- **Production**: 1 day (deploy + cutover)
- **Production Soak**: 3-7 days
- **Cleanup**: 1 day

**Total**: 2-3 weeks for complete migration

## Support and Questions

For issues during migration:
1. Check this guide's troubleshooting section
2. Review Pulumi logs: `pulumi stack --show-urns`
3. Check official APISix documentation: https://apisix.apache.org/docs/
4. Contact platform engineering team
