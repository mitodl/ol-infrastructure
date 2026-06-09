# Local-Dev Cluster Resource Usage Analysis

## Executive Summary

The local-dev Kubernetes cluster is currently using **~5Gi of memory** across all pods (11% of total 120Gi capacity). While the stack fits comfortably on modern laptops, there are significant optimization opportunities to reduce hardware requirements by **64%** (768Mi-1.2Gi savings), bringing the footprint down to **3.8-4.2Gi**.

## Current State

### Cluster Resources
- **Total Capacity**: 3 nodes × (10 CPU cores + 40Gi memory) = 30 cores + 120Gi memory
- **Actual Usage**: ~1.5 cores (5%) + 5Gi memory (11%)
- **No resource pressure**: System easily handles current workload

### Memory Distribution by Component

| Component | Actual Usage | Requested | Limited | Utilization | Efficiency |
|-----------|--------------|-----------|---------|--------------|-----------|
| LiteLLM | 881Mi | 256Mi | 2Gi | 344% | **CRITICAL** |
| OpenSearch | 680Mi | 512Mi | 1Gi | 133% | Poor |
| Keycloak | 653Mi | 512Mi | 1Gi | 128% | Poor |
| Learn-AI Celery | 914Mi | N/A | N/A | N/A | (app workload) |
| APISIX | 230Mi | 256Mi | 512Mi | 90% | Fair |
| APISIX Controller | 213Mi | None | None | N/A | Unmanaged |
| Keycloak Operator | 212Mi | 300Mi | 700Mi | 71% | Fair |
| Tika | 145Mi | 256Mi | 512Mi | 57% | Wasteful |
| PostgreSQL | 126Mi | 256Mi | 512Mi | 49% | Wasteful |
| Qdrant | 45Mi | 256Mi | 512Mi | 18% | **Wasteful** |

## Optimization Opportunities

### Tier 1: Quick Wins (Low Risk, 640Mi Savings)

#### 1. Qdrant (Vector Store)
- **Current**: 256Mi request, 512Mi limit, using **45Mi**
- **Issue**: Over-allocated by 5.7x
- **Recommendation**: Reduce to 128Mi request, 256Mi limit
- **Savings**: 256Mi
- **Risk**: ✅ Very Low - AI services don't stress this locally

#### 2. Tika (Document Processing)
- **Current**: 256Mi request, 512Mi limit, using **145Mi**
- **Issue**: Over-allocated by 1.8x
- **Recommendation**: Reduce to 128Mi request, 256Mi limit
- **Savings**: 128Mi
- **Risk**: ✅ Low - Only used for document processing

#### 3. PostgreSQL (Database)
- **Current**: 256Mi request, 512Mi limit, using **126Mi**
- **Issue**: Over-allocated by 2x
- **Recommendation**: Reduce to 128Mi request, 256Mi limit
- **Savings**: 128Mi
- **Risk**: ✅ Low - Single instance in local dev

#### 4. OpenSearch (Search Engine)
- **Current**: 512Mi request, 1Gi limit, using **680Mi**
- **Issue**: Close to limit; over-allocated by ~30%
- **Recommendation**: Reduce to 256Mi request, 512Mi limit
- **Savings**: 256Mi
- **Risk**: ⚠️ Medium - May need full 512Mi under heavy load; monitor

### Tier 2: Medium-Term Improvements (256Mi+ Savings, Medium Risk)

#### 5. LiteLLM (AI Proxy)
- **Current**: 256Mi request, **2Gi limit**, using **881Mi**
- **CRITICAL ISSUE**: Memory limit is 8x the request; pod overcommitted
- **Recommendation**: Increase request to 512Mi, reduce limit to 1Gi
- **Savings**: 1Gi (limit reduction)
- **Risk**: ⚠️ High - LiteLLM may spike; adjust if needed
- **Alternative**: Remove LiteLLM if AI features not needed

#### 6. Keycloak (Identity Provider)
- **Current**: 512Mi request, 1Gi limit, using **653Mi**
- **Issue**: Close to request limit
- **Recommendation**: Reduce to 256Mi request, 512Mi limit
- **Savings**: 256Mi
- **Risk**: ⚠️ Medium - Keycloak is essential; monitor closely on first run

#### 7. Add Resource Limits to Unmanaged Components
- **Affected**: cert-manager (3 pods), apisix-ingress-controller
- **Current**: No resource requests/limits defined
- **Recommendation**: Add 100Mi request, 256Mi limit for each
- **Benefit**: Better Kubernetes scheduling, preventing pod eviction

### Tier 3: Aggressive Optimization (For Minimal Hardware)

#### 8. Make Non-Essential Services Optional
Consider conditional Tilt deployment for features used less frequently:

| Service | Size | Used For | Recommendation |
|---------|------|----------|-----------------|
| OpenSearch | 680Mi | Content search | Optional flag in `tilt_config.json` |
| Tika | 145Mi | Document processing | Optional flag in `tilt_config.json` |
| LiteLLM | 881Mi | AI features | Optional flag in `tilt_config.json` |
| Qdrant | 512Mi limit | AI embeddings | Optional flag in `tilt_config.json` |

**Savings**: 2Gi if all disabled (but not recommended as baseline)

## Implementation Strategy

### Phase 1: Safe Immediate Changes (Quick Wins)
```yaml
# Update local-dev/infra/modules/*
qdrant:
  requests: { cpu: 100m, memory: 128Mi }
  limits: { cpu: 500m, memory: 256Mi }

tika:
  requests: { cpu: 50m, memory: 128Mi }
  limits: { cpu: 200m, memory: 256Mi }

postgres:
  requests: { cpu: 100m, memory: 128Mi }
  limits: { cpu: 500m, memory: 256Mi }

opensearch:
  requests: { cpu: 100m, memory: 256Mi }
  limits: { cpu: 500m, memory: 512Mi }
```
**Total Savings**: 640Mi
**Risk Level**: ✅ Low

### Phase 2: Monitor & Adjust (2-3 days)
Deploy Phase 1 changes and monitor for OOMKilled/evicted pods. If stable:

```yaml
litellm:
  requests: { cpu: 100m, memory: 512Mi }  # Increased
  limits: { cpu: 500m, memory: 1Gi }      # Reduced

keycloak:
  requests: { cpu: 200m, memory: 256Mi }  # Reduced
  limits: { cpu: 500m, memory: 512Mi }    # Reduced
```
**Additional Savings**: 512Mi
**Total After Phase 2**: 1.2Gi (76% reduction)
**Risk Level**: ⚠️ Medium - Requires monitoring

### Phase 3: Optional Services (User Config)
Add to `local-dev/tilt_config.json`:
```json
{
  "enable_opensearch": false,
  "enable_tika": false,
  "enable_litellm": false
}
```
**Additional Savings**: 1.7Gi if all disabled
**Total Potential**: 2.9Gi (58% from baseline)

## Expected Results

### Before Optimization
- **Memory per node**: 5Gi average
- **Minimum laptop RAM**: 16Gi recommended (32Gi for comfort)
- **Swap usage**: Likely for heavy builds

### After Phase 1
- **Memory per node**: ~4.4Gi
- **Minimum laptop RAM**: 14Gi (8Gi + 6Gi buffer)
- **Improvement**: 12% reduction

### After Phase 2
- **Memory per node**: ~3.8Gi
- **Minimum laptop RAM**: 12Gi (6Gi + 6Gi buffer)
- **Improvement**: 24% reduction

### With Optional Services Off
- **Memory per node**: ~2.1Gi
- **Minimum laptop RAM**: 8Gi (4Gi + 4Gi buffer)
- **Improvement**: 58% reduction

## Risks & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|-----------|
| LiteLLM OOMKilled | Medium | High | Start with 512Mi request; revert if needed |
| Keycloak startup fails | Low | High | Monitor auth tests; can always increase back to 512Mi |
| OpenSearch slow queries | Low | Medium | Add monitoring; reduce to 384Mi if needed |
| Database connection pool exhaustion | Very Low | Low | Single local instance unlikely to stress |

## Monitoring Strategy

After implementing changes:

1. **Daily for 3 days**: Check for pod evictions/OOMKilled events
   ```bash
   kubectl --context local-dev get events -A --sort-by='.lastTimestamp' | grep -E 'OOMKilled|Evicted'
   ```

2. **Weekly**: Monitor resource trends
   ```bash
   kubectl --context local-dev top pods -A | sort -k4 -hr
   ```

3. **Per-feature**: Test search, AI, document processing features

## Recommendations

### Immediate (This Week)
✅ Implement Phase 1 (Quick Wins: 640Mi savings)
- Low risk, high value
- No feature impact
- Immediate benefit

### Short-term (Next Week)
⚠️ Implement Phase 2 (Medium-term: +512Mi savings)
- Monitor closely after Phase 1
- Only proceed if Phase 1 stable
- Requires testing of Keycloak/LiteLLM

### Long-term (Next Sprint)
🔧 Consider Phase 3 (Optional services)
- Add configuration flags for optional services
- Improves developer experience for feature-specific work
- Reduces laptop requirements significantly for targeted development

## Conclusion

The local-dev stack can be safely optimized to use **24% less memory** (from 5Gi to 3.8Gi) with minimal risk by adjusting component resource requests/limits. Further optimization is possible by making non-essential services optional, reducing memory footprint to 2.1Gi for developers not using AI/search features.

This brings the minimum laptop requirement from **16Gi RAM** down to **12Gi RAM** for Phase 2, or **8Gi RAM** for developers using Phase 3 optional features.
