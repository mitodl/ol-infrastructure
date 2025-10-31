# APISix Migration - CI Environment Configuration Example

## Phase 1: Current State (No Changes)
```yaml
# Existing configuration in Pulumi.infrastructure.aws.eks.applications.CI.yaml
eks:apisix_ingress_enabled: "true"
eks:apisix_admin_key: <encrypted>
eks:apisix_viewer_key: <encrypted>
eks:apisix_domains:
  - gateway-ci.mitopen.net
```

## Phase 2: Enable Official Chart (Blue-Green Testing)

Add these configuration values to `Pulumi.infrastructure.aws.eks.applications.CI.yaml`:

```yaml
# Enable official APISix chart alongside Bitnami
eks:apisix_official_enabled: "true"

# Separate domain for testing official chart
eks:apisix_official_domains:
  - gateway-v2-ci.mitopen.net
```

### Deploy Phase 2
```bash
cd src/ol_infrastructure/infrastructure/aws/eks/

# Select CI stack
pulumi stack select infrastructure.aws.eks.applications.CI

# Set configuration
pulumi config set eks:apisix_official_enabled true
pulumi config set-all --plaintext 'eks:apisix_official_domains=["gateway-v2-ci.mitopen.net"]'

# Preview changes
pulumi preview

# Deploy (will create new NLB and APISix deployment)
pulumi up

# Verify deployment
kubectl -n operations get pods -l app.kubernetes.io/name=apisix
kubectl -n operations get svc | grep apisix
```

### Expected Resources After Phase 2
```
PODS:
- apisix-control-plane-xxx (Bitnami - production)
- apisix-etcd-xxx (Bitnami etcd)
- apisix-ingress-controller-xxx (Bitnami controller)
- apisix-official-xxx (Official chart - testing)
- apisix-official-etcd-xxx (Official etcd)
- apisix-official-ingress-controller-xxx (Official controller)

SERVICES:
- apisix-control-plane (Bitnami NLB - gateway-ci.mitopen.net)
- apisix-official-gateway (Official NLB - gateway-v2-ci.mitopen.net)
- apisix-official-admin (Official admin API)
```

## Phase 3: Traffic Cutover (Move Production Traffic)

Update configuration to point production domain to official chart:

```yaml
# Both charts still running
eks:apisix_ingress_enabled: "true"
eks:apisix_official_enabled: "true"

# Bitnami keeps its domain (for rollback)
eks:apisix_domains:
  - gateway-ci.mitopen.net

# Official chart gets production domain
eks:apisix_official_domains:
  - gateway-ci.mitopen.net       # Production traffic
  - gateway-v2-ci.mitopen.net    # Testing alias
```

### Deploy Phase 3
```bash
# Update configuration
pulumi config set-all --plaintext \
  'eks:apisix_official_domains=["gateway-ci.mitopen.net","gateway-v2-ci.mitopen.net"]'

# Deploy (updates Route53 DNS only, both NLBs remain)
pulumi up

# Monitor DNS propagation
watch -n 5 'dig gateway-ci.mitopen.net +short'

# Verify traffic
kubectl -n operations logs -l app.kubernetes.io/name=apisix-official --tail=50 -f
```

### Rollback Phase 3 (if issues)
```bash
# Quick rollback: remove production domain from official chart
pulumi config set-all --plaintext \
  'eks:apisix_official_domains=["gateway-v2-ci.mitopen.net"]'

pulumi up  # Reverts DNS in 1-2 minutes
```

## Phase 4: Cleanup (Remove Bitnami Chart)

After 24-48 hours of successful operation:

```yaml
# Remove Bitnami chart
eks:apisix_ingress_enabled: "false"
eks:apisix_official_enabled: "true"

# Official chart is only ingress
eks:apisix_official_domains:
  - gateway-ci.mitopen.net
  - gateway-v2-ci.mitopen.net  # Optional, can remove
```

### Deploy Phase 4
```bash
# Disable Bitnami chart
pulumi config set eks:apisix_ingress_enabled false

# Preview (will show deletion of Bitnami resources)
pulumi preview

# Deploy
pulumi up

# Verify only official chart remains
kubectl -n operations get pods -l app.kubernetes.io/name=apisix
# Should only show apisix-official-* pods
```

## Testing Checklist for Each Phase

### Phase 2 Testing (Parallel Deployment)
- [ ] Both sets of pods are running
- [ ] Two separate NLBs exist
- [ ] DNS resolves correctly for both domains
  ```bash
  dig gateway-ci.mitopen.net +short       # Old Bitnami NLB
  dig gateway-v2-ci.mitopen.net +short    # New official NLB
  ```
- [ ] Test route via official chart using new domain
- [ ] Verify prometheus metrics for both
- [ ] Check logs are being generated

### Phase 3 Testing (Traffic Cutover)
- [ ] DNS propagation completed (both domains → official NLB)
- [ ] No 5xx errors in logs
- [ ] Response times normal
- [ ] All APISix routes working
- [ ] Prometheus metrics flowing
- [ ] Can rollback quickly if needed

### Phase 4 Testing (Cleanup)
- [ ] Bitnami pods terminated
- [ ] Bitnami NLB deleted
- [ ] Only official chart pods remain
- [ ] Traffic still flowing normally
- [ ] No application disruption

## Configuration Reference

### Required Config (All Phases)
```yaml
eks:apisix_admin_key: <secret>       # Shared by both charts
eks:apisix_viewer_key: <secret>      # Shared by both charts
```

### Phase-Specific Config
```yaml
# Phase 1: Bitnami only
eks:apisix_ingress_enabled: "true"
eks:apisix_domains: [...]

# Phase 2: Blue-green
eks:apisix_ingress_enabled: "true"
eks:apisix_official_enabled: "true"
eks:apisix_domains: [...]
eks:apisix_official_domains: [...]   # Different domains

# Phase 3: Cutover
eks:apisix_ingress_enabled: "true"
eks:apisix_official_enabled: "true"
eks:apisix_domains: [...]
eks:apisix_official_domains: [...]   # Includes production domains

# Phase 4: Official only
eks:apisix_ingress_enabled: "false"
eks:apisix_official_enabled: "true"
eks:apisix_official_domains: [...]
```

## Troubleshooting Commands

```bash
# Check pod status
kubectl -n operations get pods -l app.kubernetes.io/name=apisix -o wide

# Check services and NLBs
kubectl -n operations get svc | grep apisix

# View logs
kubectl -n operations logs -l app.kubernetes.io/name=apisix-official --tail=100

# Check ingress controller
kubectl -n operations logs -l app.kubernetes.io/name=apisix-ingress-controller --tail=100

# Verify DNS
dig gateway-ci.mitopen.net +short
dig gateway-v2-ci.mitopen.net +short

# Check NLB target health (from AWS Console or CLI)
aws elbv2 describe-target-health \
  --target-group-arn <target-group-arn>

# Test connectivity
curl -v https://gateway-v2-ci.mitopen.net/health

# Check APISix admin API (from inside cluster)
kubectl -n operations exec -it <apisix-official-pod> -- \
  curl -s http://localhost:9180/apisix/admin/routes \
  -H "X-API-KEY: <admin-key>" | jq
```

## Estimated Time per Phase

- **Phase 2 Deploy**: 5-10 minutes (NLB provisioning)
- **Phase 2 Testing**: 2-4 hours
- **Phase 3 Deploy**: 2-5 minutes (DNS update only)
- **Phase 3 Soak**: 24-48 hours
- **Phase 4 Deploy**: 2-3 minutes (resource deletion)

**Total CI Migration**: 3-4 days
