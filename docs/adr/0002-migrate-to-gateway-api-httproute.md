# 0002. Migrate to Gateway API HTTPRoute from APISIX CRDs

**Status:** Proposed
**Date:** 2025-10-29
**Deciders:** Platform Team (pending approval)
**Technical Story:** External-DNS automation for APISIX ingress routes

## Context

### Current Situation

The ol-infrastructure project uses APISIX Ingress Controller with custom `ApisixRoute` CRDs to route traffic for 38 applications. External-DNS creates Route53 records based on a manually maintained list of domains in the APISIX Service annotation:

```yaml
# In src/ol_infrastructure/infrastructure/aws/eks/apisix_official.py
"external-dns.alpha.kubernetes.io/hostname": ",".join(apisix_domains)
```

The domain list is stored in `Pulumi.*.yaml` stack configs:
```yaml
eks:apisix_domains:
  - "api-pay-qa.ol.mit.edu"
  - "api-learn-ai-qa.ol.mit.edu"
  - "api.rc.learn.mit.edu"
  - "nb.rc.learn.mit.edu"
  # ... 20+ more domains
```

### Problem Statement

**Manual Synchronization Required:**
- Every time an application adds or changes a domain in their `ApisixRoute`, the EKS stack config must be manually updated
- Two separate PRs required: one for the application, one for the EKS config
- Easy to forget to update the domain list
- No automatic validation that domains match routes

**Maintenance Overhead:**
- 38 applications × multiple domains each = significant maintenance burden
- Cross-stack dependencies make deployments complex
- No single source of truth for which domains are active

**Discovery Process:**
A code comment stated "at this time 20241218, apisix does not provide first class support for the kubernetes gateway api" which led to investigation revealing Gateway API is actually production-ready.

### Business/Technical Drivers

- **Operational Efficiency:** Eliminate manual domain list maintenance
- **Reduce Errors:** Prevent domain/route mismatches
- **Future-Proof:** Align with Kubernetes standard (Gateway API is SIG-Network standard)
- **Ecosystem Benefits:** Better tooling, documentation, and community support for Gateway API

### Constraints

- Must maintain existing OIDC authentication (complex SSO flows with Keycloak)
- Must support APISIX plugins (proxy-rewrite, CORS, response-rewrite, etc.)
- Cannot cause downtime for 38 production applications
- Must work with existing Vault Secrets Operator integration
- Team capacity: ~50 hours for full migration

### Assumptions

- APISIX Ingress Controller version supports Gateway API
- Gateway API CRDs are installed in cluster
- External-DNS is configured to watch `gateway-httproute` source (already confirmed)
- Blue-green deployment approach will minimize risk

## Options Considered

### Option 1: Query ApisixRoute CRDs Dynamically

**Approach:** At Pulumi deploy time, query all `ApisixRoute` resources in the cluster and extract hostnames to populate the external-dns annotation.

**Pros:**
- ✅ Solves domain list automation (same end goal as Gateway API)
- ✅ Minimal effort (4 hours estimated)
- ✅ Zero risk to applications (no app changes)
- ✅ No breaking changes
- ✅ Works with existing ApisixRoute CRDs

**Cons:**
- ❌ Stays on proprietary APISIX CRDs (not Kubernetes standard)
- ❌ Doesn't benefit from Gateway API ecosystem
- ❌ Chicken-and-egg problem on initial cluster setup (no routes exist yet)
- ❌ Requires Pulumi to query K8s API at deploy time
- ❌ Doesn't address technical debt of non-standard APIs

**Decision:** Not chosen as primary, but kept as fallback if Gateway API migration fails during pilot phase.

### Option 2: Migrate to Gateway API HTTPRoute (CHOSEN)

**Approach:** Migrate from `ApisixRoute` CRDs to Gateway API `HTTPRoute` resources. External-DNS natively discovers domains from HTTPRoute without manual annotation.

**Pros:**
- ✅ External-DNS automation (native HTTPRoute hostname discovery)
- ✅ Kubernetes standard (Gateway API v1, SIG-Network approved)
- ✅ Future-proof architecture
- ✅ Better ecosystem support (tooling, docs, community)
- ✅ Better separation of concerns (routes vs policies)
- ✅ APISIX Gateway API support is production-ready (v1 APIs, Core Support documented)

**Cons:**
- ⚠️ Moderate effort (50 hours across 5 phases)
- ⚠️ 38 applications at risk (mitigated by phased rollout)
- ⚠️ More verbose configuration (need separate PluginConfig resources)
- ⚠️ Learning curve for team (new API patterns)
- ⚠️ OIDC complexity (need separate configs for `pass` vs `auth` modes)

**Decision:** Chosen with phased approach and pilot validation gate.

### Option 3: Switch to Different Ingress Controller

**Approach:** Migrate from APISIX to another ingress controller with better Gateway API support (e.g., NGINX, Traefik, Envoy Gateway).

**Pros:**
- ✅ Could get better Gateway API integration
- ✅ Potentially simpler configuration

**Cons:**
- ❌ Massive migration effort (100+ hours)
- ❌ Loss of APISIX-specific features we use
- ❌ High risk to all 38 applications
- ❌ Requires learning entirely new ingress controller
- ❌ May still need custom plugins/features

**Decision:** Not chosen - too risky and effort-intensive compared to benefits.

## Decision

**Migrate from APISIX ApisixRoute CRDs to Gateway API HTTPRoute using a phased approach with pilot validation.**

### Implementation Approach

**Phase 1: Enable Gateway API** (2 hours, Low risk)
- Set `enableGatewayAPI: True` in APISIX config
- Create Gateway and GatewayClass resources
- Validate existing ApisixRoutes still work

**Phase 2: Build OLHTTPRoute Component** (8 hours, Medium risk)
- Create Pulumi component with API compatible to OLApisixRoute
- Auto-generate PluginConfig resources from inline plugin configs
- Support OIDC plugin variations (pass/auth modes)

**Phase 3: Pilot Migration (3 apps)** (16 hours, Medium risk)
- Migrate 3 test applications:
  1. Simple app (no OIDC)
  2. Basic OIDC app (single mode)
  3. Complex app like mit_learn (multiple OIDC modes, path rewriting)
- **DECISION GATE:** Only proceed if pilot succeeds

**Phase 4: Bulk Migration (35 apps)** (20 hours, Low risk after pilot)
- Migrate remaining applications in 3 batches
- Use proven patterns from pilot
- Blue-green deployment per app

**Phase 5: Cleanup** (4 hours, Low risk)
- Remove manual domain lists from configs
- Remove ApisixRoute support
- Update documentation

**Total Effort:** 50 hours

### Rationale

**Why Gateway API over Option 1 (dynamic discovery):**

Gateway API provides long-term strategic benefits:
1. **Industry Standard:** Gateway API is the future of K8s ingress (v1 APIs)
2. **Ecosystem:** Growing tooling, documentation, and community support
3. **Better Architecture:** Clearer separation of routes, policies, and backend config
4. **Future Features:** Will get new capabilities as Gateway API evolves

The 46-hour difference in effort (50 vs 4 hours) is justified by:
- Eliminating technical debt of proprietary CRDs
- Positioning infrastructure for next 3-5 years
- Improving team skills on K8s standards
- Enabling future ingress controller options (Gateway API is portable)

**Why phased approach:**
- Reduces risk through pilot validation
- Can fallback to Option 1 if pilot fails
- Provides learning opportunity before bulk migration
- Allows rollback per application during migration

**Key Enabler:** Official APISIX documentation confirms Gateway API support is production-ready, not experimental as initially assumed.

## Consequences

### Positive Consequences

- ✅ **Zero Manual Domain Management:** External-DNS automatically discovers domains from HTTPRoute
- ✅ **Faster Deployments:** No EKS stack updates needed when adding domains
- ✅ **Reduced Errors:** Single source of truth (HTTPRoute) for domains
- ✅ **Future-Proof:** Aligned with Kubernetes standard
- ✅ **Better Tooling:** Gateway API has growing ecosystem support
- ✅ **Portability:** Easier to switch ingress controllers if needed (Gateway API is standard)
- ✅ **Clearer Architecture:** Routes (HTTPRoute) separate from policies (HTTPRoutePolicy) separate from backend config (BackendTrafficPolicy)

### Negative Consequences

- ❌ **Migration Effort:** 50 hours engineering time
- ❌ **Risk During Migration:** 38 applications affected (mitigated by phased approach)
- ❌ **More Resources:** Need separate PluginConfig for OIDC variations (~76 resources vs 38)
- ❌ **Configuration Verbosity:** HTTPRoute + PluginConfig + HTTPRoutePolicy vs single ApisixRoute
- ❌ **Learning Curve:** Team needs to learn Gateway API concepts
- ❌ **Tooling Gap:** Some APISIX-specific features still require CRDs (PluginConfig, BackendTrafficPolicy)

### Neutral Consequences

- ⚪ **OIDC Complexity:** Need separate PluginConfigs for `pass` vs `auth` modes (more explicit, not necessarily worse)
- ⚪ **Debugging Changes:** New patterns for troubleshooting Gateway API resources
- ⚪ **Documentation Updates:** Need to update team docs and onboarding materials

## Implementation Notes

### Technical Details

**Gateway API Resources Used:**
- `GatewayClass` - Defines APISIX as the controller
- `Gateway` - Represents the APISIX ingress point
- `HTTPRoute` - Replaces `ApisixRoute` for HTTP routing

**APISIX CRDs Still Used:**
- `PluginConfig` (v1alpha1) - For plugins via ExtensionRef
- `HTTPRoutePolicy` (v1alpha1) - For priority and advanced matching
- `BackendTrafficPolicy` (v1alpha1) - For load balancing, retries, timeouts

**Example HTTPRoute:**
```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: mitlearn-route
spec:
  parentRefs:
  - name: apisix
  hostnames:
  - api.rc.learn.mit.edu  # External-DNS discovers this automatically
  rules:
  - matches:
    - path: {type: PathPrefix, value: /learn}
    filters:
    - type: ExtensionRef
      extensionRef:
        kind: PluginConfig
        name: mitlearn-oidc-pass
    backendRefs:
    - name: mitlearn-service
      port: 8000
```

### Effort Breakdown

| Phase | Hours | Activities |
|-------|-------|------------|
| 1. Enable Gateway API | 2 | Config change, validation |
| 2. Build Component | 8 | Code, tests, review |
| 3. Pilot (3 apps) | 16 | Migration, testing, validation |
| **DECISION GATE** | - | GO/NO-GO based on pilot |
| 4. Bulk (35 apps) | 20 | Migration in batches |
| 5. Cleanup | 4 | Remove old configs, docs |
| **Total** | **50** | |

### Risk Mitigation

- **Pilot Phase:** Validates approach before bulk migration
- **Blue-Green per App:** Both ApisixRoute and HTTPRoute exist during migration
- **DNS Cutover:** Control which is active (quick rollback)
- **24-Hour Soak:** Monitor each app before removing ApisixRoute
- **Fallback:** Can stop after pilot and use Option 1 instead

### Success Criteria

**Technical:**
- ✅ All 38 applications migrated successfully
- ✅ External-DNS creates Route53 records automatically
- ✅ OIDC authentication works (all modes)
- ✅ No latency regression (p95 < baseline)
- ✅ No error rate increase

**Operational:**
- ✅ Zero user-facing incidents
- ✅ Zero manual domain list updates needed
- ✅ Team trained on Gateway API

## Related Decisions

- **ADR-0001:** Use ADR for Architecture Decisions (this process)
- **Related Plans:** [Gateway API Migration Plan](../gateway-api-migration-plan.md)
- **Related Code:**
  - [APISIX Official Config](../../src/ol_infrastructure/infrastructure/aws/eks/apisix_official.py)
  - [External-DNS Config](../../src/ol_infrastructure/infrastructure/aws/eks/external_dns.py)
  - [OLApisixRoute Component](../../src/ol_infrastructure/components/services/k8s.py)

## References

### APISIX Documentation
- [APISIX Gateway API Concepts](https://apisix.apache.org/docs/ingress-controller/concepts/gateway-api/) - Confirms production-ready support
- [APISIX Gateway API Examples](https://apisix.apache.org/docs/ingress-controller/reference/example/) - Shows PluginConfig pattern
- [APISIX API Reference](https://apisix.apache.org/docs/ingress-controller/reference/api-reference/) - API specifications

### Gateway API Documentation
- [Gateway API Specification](https://gateway-api.sigs.k8s.io/)
- [Gateway API Use Cases](https://gateway-api.sigs.k8s.io/guides/)
- [External-DNS Gateway API Support](https://github.com/kubernetes-sigs/external-dns/blob/master/docs/tutorials/gateway-api.md)

### Analysis Documents
- [Initial Analysis](/tmp/gateway_api_analysis.md) - First evaluation (assumed experimental)
- [Updated Analysis](/tmp/gateway_api_reevaluation.md) - After reading official docs

## Notes

### Discovery Process

This decision emerged from analyzing external-dns domain management. Initial assumption was that Gateway API support was experimental based on a code comment from Dec 2024. Reading official APISIX documentation revealed:
- HTTPRoute is v1 stable with "Core Support = Supported"
- PluginConfig via ExtensionRef is officially documented
- All APISIX plugins work with Gateway API (including OIDC)

This changed the recommendation from "DO NOT MIGRATE" to "PROCEED WITH PHASED MIGRATION".

### Alternative Scenarios

**If Pilot Fails:**
- Stop after Phase 3
- Implement Option 1 (dynamic ApisixRoute query, 4 hours)
- Re-evaluate Gateway API in 6 months

**If Partial Migration Desired:**
- Migrate new applications to Gateway API
- Keep existing apps on ApisixRoute
- Run both patterns in parallel (not recommended long-term)

### Open Questions

1. **APISIX Ingress Controller Version:** Need to verify cluster has version supporting Gateway API
2. **Gateway API CRD Installation:** Need to confirm CRDs exist in cluster
3. **Team Capacity:** Need to confirm 50 hours available for this effort
4. **Priority:** Is this higher priority than other infrastructure work?

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2025-10-29 | GitHub Copilot | Proposed | Created during agentic analysis session |
| _TBD_ | _Platform Team_ | _Pending_ | Needs GO/NO-GO decision and pilot approval |

**Last Updated:** 2025-10-29
