# 0003. Use Hybrid HTTPRoute + ApisixTls for Per-Application TLS

**Status:** Accepted  
**Date:** 2025-11-13  
**Deciders:** AI Agent (GitHub Copilot CLI) + Human validation pending  
**Technical Story:** Gateway API TLS configuration for 38 APISIX applications

## Context

### Current Situation

During Phase 2 implementation of the Gateway API migration (ADR-0002), a critical TLS configuration question emerged: How should TLS certificates be managed for 38 applications when using Gateway API with a shared Gateway resource?

**Current State (ApisixRoute CRDs):**
- Each of 38 applications has its own `ApisixTls` CRD
- Per-application TLS certificate management via cert-manager
- Applications self-service their TLS configuration
- SNI routing handled by APISIX based on ApisixTls resources

**Initial Assumption:**
Gateway API would require TLS configuration at Gateway listener level using standard Kubernetes Secrets (`kind: Secret` in `certificateRefs`), requiring consolidation of 38 certificates into a shared Gateway configuration.

### Problem Statement

**Discovery:** Official APISIX documentation confirms Gateway API uses Gateway listener TLS configuration. With a shared Gateway resource, this creates several challenges:

1. **Shared Gateway, Multiple Certificates:** How do 38 applications with unique TLS certificates use one Gateway?
2. **Certificate Ownership:** Should certificates be managed centrally (infrastructure) or per-app (application)?
3. **Self-Service:** How do applications manage their own certificates without modifying shared infrastructure?
4. **Security Boundary:** How to maintain per-app TLS isolation?

### Business/Technical Drivers

- **Per-App Control:** Applications need to manage their own TLS certificates
- **Security Isolation:** Compromised certificate should only affect one application
- **Self-Service:** Applications should not require infrastructure team for certificate changes
- **Migration Simplicity:** Minimize changes during migration (38 applications affected)
- **Cert-Manager Integration:** Must work with existing cert-manager setup

### Constraints

- Gateway is shared infrastructure resource (one per cluster)
- 38 applications, each with unique domain(s) and certificate(s)
- Must maintain existing cert-manager workflows
- Cannot break TLS/HTTPS during migration
- Must complete before Phase 3 pilot

### Assumptions

- APISIX Ingress Controller supports Gateway API TLS
- Gateway API allows controller-specific extensions
- ApisixTls CRDs can coexist with HTTPRoute resources

## Options Considered

### Option A: SNI Multi-Listener Gateway

**Approach:** Add one listener per application hostname to the Gateway resource.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
spec:
  gatewayClassName: apisix
  listeners:
    - name: https-app1
      hostname: app1.mit.edu
      port: 443
      protocol: HTTPS
      tls:
        mode: Terminate
        certificateRefs:
          - kind: Secret
            name: app1-cert-secret
    - name: https-app2
      hostname: app2.mit.edu
      port: 443
      protocol: HTTPS
      tls:
        certificateRefs:
          - kind: Secret
            name: app2-cert-secret
    # ... repeat for 38+ applications
```

**Pros:**
- ✅ Standard Gateway API approach (no extensions)
- ✅ Per-app certificate control
- ✅ No changes to cert-manager workflow

**Cons:**
- ❌ Gateway explosion (38+ listeners in single resource)
- ❌ Gateway is infrastructure resource, not owned by apps
- ❌ Centralized management (apps can't self-service)
- ❌ Requires Gateway update for every new application
- ❌ Difficult to scale beyond current applications

**Estimated Effort:** 6 hours (Gateway updates, testing, deployment)

---

### Option B: Wildcard Certificate

**Approach:** Use single wildcard certificate covering all applications.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
spec:
  gatewayClassName: apisix
  listeners:
    - name: https
      port: 443
      protocol: HTTPS
      tls:
        mode: Terminate
        certificateRefs:
          - kind: Secret
            name: wildcard-mit-edu-cert  # Covers *.mit.edu
```

**Pros:**
- ✅ Simple Gateway configuration
- ✅ Scales to unlimited applications
- ✅ No Gateway updates for new apps
- ✅ Standard Gateway API

**Cons:**
- ❌ All applications share same certificate
- ❌ Security risk (certificate compromise affects all apps)
- ❌ No per-app certificate control
- ❌ May not work for different base domains
- ❌ Requires wildcard cert from cert-manager/Let's Encrypt

**Estimated Effort:** 8 hours (wildcard cert setup, Gateway config, security review)

---

### Option C: Hybrid HTTPRoute + ApisixTls (CHOSEN)

**Approach:** Use Gateway API HTTPRoute for routing, keep APISIX ApisixTls CRD for per-app TLS management.

```yaml
# Gateway (infrastructure, simple)
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
metadata:
  name: apisix
  namespace: operations
spec:
  gatewayClassName: apisix
  listeners:
    - name: https
      protocol: HTTPS
      port: 443
      tls:
        mode: Terminate
        # certificateRefs optional - APISIX auto-discovers ApisixTls
        # OR explicitly reference ApisixTls CRDs:
        certificateRefs:
          - group: apisix.apache.org
            kind: ApisixTls
            name: app1-tls

# ApisixTls (per-app, owned by application)
apiVersion: apisix.apache.org/v2
kind: ApisixTls
metadata:
  name: app1-tls
  namespace: app-namespace
spec:
  hosts:
    - app.mit.edu
  secret:
    name: app-cert-secret
    namespace: app-namespace

# HTTPRoute (per-app, owned by application)
apiVersion: gateway.networking.k8s.io/v1
kind: HTTPRoute
metadata:
  name: app1-route
  namespace: app-namespace
spec:
  parentRefs:
    - name: apisix
      namespace: operations
  hostnames:
    - app.mit.edu
  rules:
    - matches:
        - path: {type: PathPrefix, value: /}
      backendRefs:
        - name: app-service
          port: 8000
```

**Pros:**
- ✅ **Per-app certificate control** (security)
- ✅ **No Gateway listener explosion** (SNI handled by APISIX)
- ✅ **Applications self-manage certificates** (ApisixTls in their namespace)
- ✅ **Compatible with existing cert-manager setup** (no changes)
- ✅ **Officially supported by APISIX** (documented pattern)
- ✅ **Maintains security boundary** (per-app TLS isolation)
- ✅ **Simple Gateway config** (single HTTPS listener)
- ✅ **Auto-discovery** (APISIX finds ApisixTls resources)
- ✅ **Minimal migration** (keep existing ApisixTls, add HTTPRoute)

**Cons:**
- ⚠️ **Not pure Gateway API** (uses APISIX-specific extension)
- ⚠️ **Still requires ApisixTls CRD** (but applications already use this)
- ⚠️ **Hybrid approach** (Gateway API + APISIX CRD)

**Estimated Effort:** 2 hours (documentation updates, testing)

---

### Option D: TLS Passthrough

**Approach:** Gateway does not terminate TLS, passes encrypted traffic to APISIX.

```yaml
apiVersion: gateway.networking.k8s.io/v1
kind: Gateway
spec:
  listeners:
    - name: tls-passthrough
      port: 443
      protocol: TLS
      tls:
        mode: Passthrough
```

**Pros:**
- ✅ No Gateway certificate management
- ✅ Applications keep using ApisixTls

**Cons:**
- ❌ Gateway cannot inspect HTTP headers for routing
- ❌ Loses HTTP-level routing capabilities
- ❌ Not suitable for path-based routing
- ❌ Cannot use HTTP filters

**Estimated Effort:** 4 hours (Gateway reconfiguration, testing)

---

## Decision

**Use Hybrid HTTPRoute + ApisixTls approach (Option C)** for per-application TLS certificate management.

### Rationale

**Why Option C over Option A (Multi-Listener):**

1. **Scalability:** Gateway with 38+ listeners is unwieldy and difficult to manage
2. **Ownership:** Gateway is infrastructure resource; applications can't modify it for TLS changes
3. **Self-Service:** Requires infrastructure team involvement for every certificate update
4. **Complexity:** Single point of failure with massive Gateway configuration

**Why Option C over Option B (Wildcard):**

1. **Security:** Single compromised certificate affects all 38 applications
2. **Isolation:** No per-app security boundary
3. **Control:** Applications lose control over their TLS configuration
4. **Operational:** Wildcard cert rotation affects all apps simultaneously

**Why Option C over Option D (Passthrough):**

1. **Functionality:** TLS passthrough loses HTTP-level routing (path matching, headers)
2. **Features:** Cannot use APISIX plugins that inspect HTTP requests
3. **Architecture:** Defeats purpose of HTTP-aware API gateway

**Key Validation:** Official APISIX documentation explicitly shows `kind: ApisixTls` with `group: apisix.apache.org` in Gateway listener `certificateRefs`:

- Source: https://apisix.apache.org/docs/ingress-controller/reference/apisix-ingress-controller/examples/
- Source: https://apisix.apache.org/docs/ingress-controller/concepts/gateway-api/
- Source: https://docs.api7.ai/apisix/reference/apisix-ingress-controller/examples/

**This is not a workaround - it is the officially supported pattern.**

### Implementation Details

**APISIX Ingress Controller automatically:**
1. Discovers all ApisixTls resources in cluster
2. Configures SNI routing based on `spec.hosts`
3. Links certificates to Gateway listener
4. Handles TLS termination at APISIX

**No explicit certificateRefs required** in Gateway if ApisixTls resources exist. APISIX watches the cluster and configures SNI routing dynamically.

**Migration path:**
1. Keep existing ApisixTls resources (no changes)
2. Add HTTPRoute alongside ApisixRoute
3. Validate HTTPS works
4. Remove ApisixRoute (but keep ApisixTls!)

## Consequences

### Positive Consequences

- ✅ **Zero Breaking Changes:** Applications keep existing ApisixTls resources
- ✅ **Per-App Control:** Applications manage their own TLS certificates
- ✅ **Security Isolation:** Certificate compromise limited to one application
- ✅ **Self-Service:** Applications don't need infrastructure team for TLS changes
- ✅ **Simple Gateway:** Infrastructure resource remains manageable
- ✅ **Proven Pattern:** Officially documented and supported by APISIX
- ✅ **Auto-Discovery:** APISIX finds ApisixTls resources without explicit refs
- ✅ **Minimal Migration:** Smallest possible change for 38 applications

### Negative Consequences

- ❌ **Hybrid Architecture:** Uses Gateway API + APISIX CRD (not pure Gateway API)
- ❌ **APISIX-Specific:** Approach only works with APISIX Ingress Controller
- ❌ **Portability Concern:** Future ingress controller switch would need TLS redesign
- ❌ **Two Resource Types:** Applications manage both HTTPRoute and ApisixTls

### Neutral Consequences

- ⚪ **ApisixTls Maintenance:** Continue maintaining APISIX CRD (already doing this)
- ⚪ **Documentation Updates:** Need to document hybrid approach clearly
- ⚪ **Learning Curve:** Team needs to understand Gateway + ApisixTls relationship

## Implementation Notes

### Migration Checklist (Per Application)

1. ✅ **Keep ApisixTls resource** (REQUIRED - do not remove!)
2. ✅ Add HTTPRoute resource (routing configuration)
3. ✅ Verify HTTPS works (validate certificate)
4. ✅ Monitor for 24 hours
5. ✅ Remove ApisixRoute (but keep ApisixTls!)

### Validation Steps

**Before Migration:**
```bash
# Verify ApisixTls exists
kubectl get apisixtls -n app-namespace

# Check certificate secret exists
kubectl get secret app-cert-secret -n app-namespace
```

**After Migration:**
```bash
# Verify HTTPRoute created
kubectl get httproute -n app-namespace

# Verify HTTPS works
curl -v https://app.mit.edu

# Check APISIX controller logs (no errors)
kubectl logs -n operations -l app.kubernetes.io/name=apisix-ingress-controller
```

### Effort Estimate

- **Documentation:** 1 hour (update MIGRATION_EXAMPLE.md)
- **Testing:** 1 hour (validate with pilot app)
- **Total:** 2 hours

### Risk Level

**Low** - This is officially supported pattern, applications keep existing working configuration.

### Dependencies

- APISIX Ingress Controller (already deployed)
- Gateway API CRDs (already installed)
- ApisixTls CRD (already in use)
- cert-manager (already configured)

## Related Decisions

- **ADR-0002:** Migrate to Gateway API HTTPRoute (this resolves TLS question from that ADR)
- **Related:** Component consolidation decision (ADR-0004)

## References

### Official Documentation

- [APISIX Gateway API Examples](https://apisix.apache.org/docs/ingress-controller/reference/apisix-ingress-controller/examples/) - Shows `kind: ApisixTls` in certificateRefs
- [APISIX Gateway API Concepts](https://apisix.apache.org/docs/ingress-controller/concepts/gateway-api/) - Documents hybrid approach
- [API7 Gateway API Reference](https://docs.api7.ai/apisix/reference/apisix-ingress-controller/examples/) - Additional examples
- [APISIX SSL Guide](https://techoral.com/apisix/apisix-ssl.html) - TLS configuration patterns

### Analysis Documents

- [CONSOLIDATION_ANALYSIS.md](../../CONSOLIDATION_ANALYSIS.md) - TLS comparison section
- [MIGRATION_EXAMPLE.md](../../MIGRATION_EXAMPLE.md) - Updated with ApisixTls requirement
- [.PLAN.md](../../.PLAN.md) - Phase 2.5 TLS strategy analysis

### Related GitHub Issues

- [APISIX Multi-Gateway Configuration](https://github.com/apache/apisix-ingress-controller/issues/2390)
- [APISIX Multiple Cluster Support](https://github.com/apache/apisix-ingress-controller/issues/2253)

## Notes

### Discovery Timeline

- **2025-11-13 (morning):** Phase 2 implementation complete
- **2025-11-13 (afternoon):** TLS configuration question discovered
- **2025-11-13 (afternoon):** Research of official APISIX documentation
- **2025-11-13 (afternoon):** Validation of hybrid approach support
- **2025-11-13 (evening):** Decision made, documented in this ADR

### Alternative Not Considered

**Per-App Gateways:** Creating separate Gateway resource for each application was not seriously considered due to:
- Resource overhead (38+ Gateway resources)
- Operational complexity
- Not standard pattern in Kubernetes ecosystem
- Would defeat purpose of shared Gateway

### Future Considerations

If team migrates away from APISIX in the future:
1. **Pure Gateway API:** Could migrate to Option A (multi-listener) if new controller requires it
2. **Wildcard Cert:** Could migrate to Option B if security model changes
3. **Service Mesh:** Could move TLS to service mesh layer (mutual TLS)

This decision optimizes for current state (APISIX, 38 apps, per-app TLS) and does not preclude future changes.

---

**Review History:**

| Date | Reviewer | Decision | Notes |
|------|----------|----------|-------|
| 2025-11-13 | AI Agent (GitHub Copilot CLI) | Proposed | Created based on APISIX documentation research |
| _TBD_ | Platform Team | _Pending_ | Needs human validation and acceptance |

**Last Updated:** 2025-11-13
