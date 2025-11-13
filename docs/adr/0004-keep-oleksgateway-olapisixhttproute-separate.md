# 0004. Keep OLEKSGateway and OLApisixHTTPRoute Components Separate

**Status:** Accepted  
**Date:** 2025-11-13  
**Deciders:** AI Agent (GitHub Copilot CLI) + Human validation pending  
**Technical Story:** Consolidation analysis for Gateway API components

## Context

### Current Situation

The ol-infrastructure repository contains two Pulumi components that both create Gateway API resources:

1. **OLEKSGateway** (in `src/ol_infrastructure/components/aws/eks.py`)
   - Creates Gateway + HTTPRoute resources for Traefik Ingress Controller
   - Used by 5 applications: airbyte, keycloak, mit_learn_nextjs, open_metadata, superset
   - Provides TLS termination with cert-manager integration
   - Simple HTTP/HTTPS routing without authentication

2. **OLApisixHTTPRoute** (in `src/ol_infrastructure/components/services/apisix_gateway_api.py`)
   - Creates HTTPRoute resources for APISIX Ingress Controller
   - Target: 38 applications currently using ApisixRoute CRD
   - Provides complex routing with OIDC authentication, rate limiting, and APISIX plugins
   - Part of Gateway API migration effort (ADR-0002)

Both components use Gateway API (`gateway.networking.k8s.io/v1`) resources, raising the question: **Should these components be consolidated?**

### Problem Statement

**Surface-Level Similarity:**
- Both create `HTTPRoute` resources
- Both use Gateway API standard
- Both integrate with cert-manager for TLS
- Both create per-application routing configuration

**Discovery Trigger:**
During Phase 2 implementation of ADR-0002, consolidation analysis was requested to avoid duplication and simplify the codebase.

### Business/Technical Drivers

- **Code Maintainability:** Reduce duplication if components serve same purpose
- **Team Efficiency:** Single pattern easier to understand and maintain
- **Architectural Consistency:** Consistent approach across infrastructure
- **Future Flexibility:** Easier to switch ingress controllers if unified

### Constraints

- Cannot break existing 5 Traefik-based applications (OLEKSGateway)
- Cannot block migration of 38 APISIX applications (OLApisixHTTPRoute)
- Must maintain per-app certificate control (security requirement)
- Must support OIDC authentication (business requirement)
- Team capacity limited (~50 hours for APISIX migration)

### Assumptions

- Gateway API is sufficiently standardized for consolidation
- Ingress controller differences can be abstracted
- TLS configuration approaches can be unified
- Plugin systems can be abstracted or made optional

## Analysis Summary

A comprehensive analysis was conducted examining:
1. Architecture differences
2. Ingress controller compatibility
3. TLS configuration approaches
4. Plugin system integration
5. Configuration model differences
6. Code overlap potential

**Full analysis:** `CONSOLIDATION_ANALYSIS.md` (16 KB, detailed breakdown)

## Options Considered

### Option A: Merge into Single Component

**Approach:** Create unified `OLGatewayRoute` component supporting both Traefik and APISIX.

```python
OLGatewayRoute(
    gateway_class="traefik",  # or "apisix"
    name="app-route",
    route_configs=[...],
    plugins=[...],  # Optional, only for APISIX
)
```

**Pros:**
- ✅ Single component to maintain
- ✅ Unified API for applications
- ✅ Potential code reuse

**Cons:**
- ❌ Plugin systems incompatible (Traefik filters vs APISIX ExtensionRef)
- ❌ TLS handling fundamentally different (Gateway TLS vs ApisixTls CRD)
- ❌ Configuration models incompatible (listener-bound vs gateway-attached)
- ❌ Requires massive abstraction layer
- ❌ Obscures controller-specific features
- ❌ Would break existing OLEKSGateway users (5 apps)

**Estimated Effort:** 40 hours (abstraction layer, migration, testing)

---

### Option B: Make OLApisixHTTPRoute Support Traefik

**Approach:** Add `gateway_class` parameter to OLApisixHTTPRoute.

```python
OLApisixHTTPRoute(
    gateway_class="traefik",  # Add Traefik support
    name="app-route",
    route_configs=[...],
)
```

**Pros:**
- ✅ Single new component
- ✅ Applications choose controller

**Cons:**
- ❌ Traefik doesn't support ApisixPluginConfig CRDs
- ❌ Would break plugin system (core APISIX feature)
- ❌ Requires separate plugin system for Traefik
- ❌ Violates single responsibility principle
- ❌ Defeats purpose of controller-specific optimization

**Estimated Effort:** 30 hours (Traefik plugin system, testing)

---

### Option C: Deprecate OLEKSGateway in Favor of OLApisixHTTPRoute

**Approach:** Migrate 5 Traefik applications to APISIX.

**Pros:**
- ✅ Single ingress controller
- ✅ Single component to maintain
- ✅ Unified approach

**Cons:**
- ❌ OLEKSGateway apps don't need APISIX complexity
- ❌ Requires TLS restructuring (Gateway listener → ApisixTls CRD)
- ❌ Creates unnecessary APISIX dependency for simple use cases
- ❌ Migration effort for 5 working applications
- ❌ No clear benefit to justify migration

**Estimated Effort:** 20 hours (5 app migrations)

---

### Option D: Keep Components Separate (CHOSEN)

**Approach:** Maintain both components as controller-specific implementations.

**Rationale:**
- **OLEKSGateway:** Optimized for Traefik (simple HTTPS, per-app Gateway)
- **OLApisixHTTPRoute:** Optimized for APISIX (complex routing, plugins, shared Gateway)

**Pros:**
- ✅ No breaking changes to existing applications
- ✅ Clear separation of concerns
- ✅ Controller-specific optimizations
- ✅ Easier to understand (explicit not abstract)
- ✅ Each component optimized for its use case
- ✅ Minimal effort (documentation only)

**Cons:**
- ⚠️ Two components to maintain (but minimal overlap)
- ⚠️ Slightly more documentation needed

**Estimated Effort:** 2 hours (documentation)

---

## Decision

**Keep OLEKSGateway and OLApisixHTTPRoute as separate, controller-specific components.**

### Rationale

**These components serve fundamentally different purposes:**

| Aspect | OLEKSGateway | OLApisixHTTPRoute |
|--------|--------------|-------------------|
| **Ingress Controller** | Traefik | APISIX |
| **Use Case** | Simple HTTPS proxy | Complex routing with plugins |
| **Gateway Ownership** | Per-application | Shared (cluster-wide) |
| **TLS Approach** | Gateway listener (Secret) | ApisixTls CRD |
| **Plugins** | None (standard filters) | APISIX ecosystem (50+ plugins) |
| **Authentication** | None | OIDC, OAuth, JWT, etc. |
| **Applications** | 5 apps | 38 apps (migrating) |

**Key Finding: Only 5% Code Overlap**

Analysis of both components revealed:
- **Shared:** Kubernetes API calls (`pulumi_kubernetes.apiextensions.CustomResource`)
- **Unique to OLEKSGateway:** TLS listener config, certificate secret mgmt, HTTP→HTTPS redirect, listener hostname binding
- **Unique to OLApisixHTTPRoute:** ApisixPluginConfig generation, plugin deduplication, ExtensionRef filters, path conversion, hostname extraction

**No meaningful consolidation possible without:**
1. Massive abstraction layer (40+ hours)
2. Breaking existing applications (5 Traefik apps)
3. Obscuring controller-specific features
4. Making both components worse

**Analogy:** These are as different as:
- ALBIngressController vs NGINXIngressController
- CloudFront vs APISIX (different network layers)
- S3 vs EBS (different storage paradigms)

Both use Gateway API, but for **different ingress controllers with incompatible plugin systems**.

### Critical Incompatibilities

**1. Gateway Class**
```python
# OLEKSGateway - Hardcoded to Traefik
gateway_class_name: str = "traefik"
if gateway_class_name != "traefik":
    raise ValueError("Only 'traefik' is supported")

# OLApisixHTTPRoute - Expects APISIX
gateway_name: str = "apisix"  # References Gateway with gatewayClassName: apisix
```

**2. Plugin Systems**
```yaml
# Traefik: Standard Gateway API filters
filters:
  - type: RequestRedirect
    requestRedirect: {scheme: https}

# APISIX: ExtensionRef to APISIX CRDs
filters:
  - type: ExtensionRef
    extensionRef:
      group: apisix.apache.org
      kind: ApisixPluginConfig
```

**3. TLS Configuration**
```yaml
# OLEKSGateway: Gateway listener with Secret
listeners:
  - protocol: HTTPS
    tls:
      certificateRefs:
        - kind: Secret
          name: app-cert

# OLApisixHTTPRoute: ApisixTls CRD (per ADR-0003)
# Gateway listener references:
listeners:
  - protocol: HTTPS
    tls:
      certificateRefs:
        - group: apisix.apache.org
          kind: ApisixTls
```

**4. Gateway Ownership**
```python
# OLEKSGateway: Creates per-app Gateway
gateway = kubernetes.apiextensions.CustomResource(
    f"{app_name}-gateway",
    kind="Gateway",
    # Application owns this Gateway
)

# OLApisixHTTPRoute: References shared Gateway
spec={
    "parentRefs": [{
        "name": "apisix",  # Shared Gateway in operations namespace
        "namespace": "operations",
    }]
}
```

These are **architectural differences, not implementation details**.

## Consequences

### Positive Consequences

- ✅ **No Breaking Changes:** 5 Traefik applications continue working
- ✅ **No Migration Delays:** 38 APISIX applications can migrate without blocking
- ✅ **Clear Separation:** Each component optimized for its controller
- ✅ **Easier Understanding:** Explicit controller choice, not abstraction
- ✅ **Maintainability:** Each component simpler without abstraction layer
- ✅ **Future Flexibility:** Can optimize each independently

### Negative Consequences

- ❌ **Two Components:** Team maintains both (but minimal overlap)
- ❌ **Documentation Needed:** Must document when to use each
- ❌ **Slight Redundancy:** Both call Kubernetes APIs (unavoidable)

### Neutral Consequences

- ⚪ **Decision Matrix Needed:** Help teams choose the right component
- ⚪ **Component Naming:** Could be more explicit (e.g., `OLTraefikGateway` vs `OLEKSGateway`)

## Implementation Notes

### Documentation Requirements

**Decision Matrix for Teams:**

| Need | Use |
|------|-----|
| Simple HTTPS proxy, no auth | OLEKSGateway (Traefik) |
| OIDC/OAuth authentication | OLApisixHTTPRoute (APISIX) |
| Rate limiting, circuit breaker | OLApisixHTTPRoute (APISIX) |
| Request/response transformation | OLApisixHTTPRoute (APISIX) |
| Per-app Gateway isolation | OLEKSGateway (Traefik) |
| Shared Gateway (many apps) | OLApisixHTTPRoute (APISIX) |

### Consolidation Reconsidered If:

Re-evaluate this decision if:
1. **Traefik adoption grows** (>10 applications) - Would justify Traefik-specific optimizations
2. **APISIX adoption shrinks** (<10 applications) - Would justify consolidation effort
3. **Gateway API plugin standard emerges** - Could enable true abstraction
4. **Team switches to single ingress controller** - Would naturally eliminate one component

### Effort Estimate

- **Keep Separate (Chosen):** 2 hours (documentation)
- **Consolidate:** 40+ hours (abstraction, migration, testing, debugging)

**Savings: 38 hours** by keeping separate

### Risk Level

**Very Low** - No code changes, no application impact, no new complexity.

## Related Decisions

- **ADR-0002:** Migrate to Gateway API HTTPRoute (created OLApisixHTTPRoute)
- **ADR-0003:** Use Hybrid HTTPRoute + ApisixTls for TLS (APISIX-specific, further validates separation)

## References

### Analysis Documents

- **[CONSOLIDATION_ANALYSIS.md](../../CONSOLIDATION_ANALYSIS.md)** - 16 KB comprehensive analysis
  - Architecture comparison
  - Incompatibility analysis (4 critical areas)
  - Use case mapping
  - 4 consolidation scenarios evaluated
  - Shared code analysis (<5% overlap)
  - Impact assessment

### Component Code

- **OLEKSGateway:** `src/ol_infrastructure/components/aws/eks.py` (lines 18-281)
- **OLApisixHTTPRoute:** `src/ol_infrastructure/components/services/apisix_gateway_api.py` (285 lines)

### Usage Examples

- **OLEKSGateway:** `src/ol_infrastructure/applications/open_metadata/__main__.py`
- **OLApisixHTTPRoute:** `MIGRATION_EXAMPLE.md` (celery_monitoring example)

### Official Documentation

- [Gateway API Specification](https://gateway-api.sigs.k8s.io/)
- [Traefik Gateway API Guide](https://doc.traefik.io/traefik/routing/providers/kubernetes-gateway/)
- [APISIX Gateway API Concepts](https://apisix.apache.org/docs/ingress-controller/concepts/gateway-api/)

## Notes

### Analysis Process

1. **Initial request:** "Review and identify consolidation opportunities"
2. **Comparison:** Architecture, TLS, plugins, config models
3. **Discovery:** Only 5% code overlap, 4 critical incompatibilities
4. **Validation:** TLS approaches differ even more than initially thought (ADR-0003)
5. **Conclusion:** Components serve fundamentally different purposes

### Key Insight

**"Gateway API" does not mean "interchangeable"**

Both components use Gateway API standard, but:
- Gateway API is a framework, not a complete solution
- Controllers extend Gateway API with custom resources (PluginConfig, ApisixTls, etc.)
- Plugin systems are controller-specific
- TLS approaches differ by controller

**Consolidation would create an abstraction that hides controller-specific features without providing value.**

### Future Considerations

If the team standardizes on a single ingress controller:
- **All Traefik:** Deprecate OLApisixHTTPRoute, migrate APISIX apps to Traefik
- **All APISIX:** Deprecate OLEKSGateway, migrate Traefik apps to APISIX

Until then, maintaining both is the pragmatic choice.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2025-11-13 | AI Agent (GitHub Copilot CLI) | Proposed | Based on comprehensive analysis (CONSOLIDATION_ANALYSIS.md) |
| _TBD_ | Platform Team | _Pending_ | Needs human validation and acceptance |

**Last Updated:** 2025-11-13
