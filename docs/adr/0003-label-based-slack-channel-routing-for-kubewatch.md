# 0003. Label-Based Slack Channel Routing for Kubewatch

**Status:** Proposed
**Date:** 2025-11-20
**Deciders:** AI Agent + Human Reviewer
**Technical Story:** [Implementation Session - Label-Based Slack Channel Routing]

## Context

The kubewatch webhook handler currently sends all deployment notifications to a single Slack channel per environment (e.g., `#kubewatch-applications-ci`). As the number of applications and teams grows, this creates notification noise and makes it difficult for teams to focus on their own deployments.

### Problem Statement

Teams want to receive kubewatch deployment notifications in their own Slack channels without:
- Deploying separate kubewatch instances per team
- Managing complex routing configuration files
- Losing notifications if routing fails
- Requiring infrastructure changes for each new team

### Current Architecture

```
Kubewatch → Webhook Handler → Single Slack Channel per Environment
```

All deployments in the CI environment go to `#kubewatch-applications-ci`, regardless of which team owns the deployment.

### Requirements

1. Route notifications to different Slack channels based on deployment
2. Graceful fallback to default channel if routing fails
3. No additional infrastructure or configuration management overhead
4. 100% backward compatible with existing deployments
5. Self-service for teams (no DevOps bottleneck)

### Options Considered

#### Option 1: Multiple Kubewatch Instances

Deploy separate kubewatch + webhook handler pairs per team.

**Pros:**
- Complete isolation between teams
- No code changes required

**Cons:**
- Operational complexity (N instances to manage)
- Resource overhead (N×2 pods, N deployments)
- Doesn't scale beyond 3-5 teams
- Harder to maintain and update

**Verdict:** ❌ Not scalable

#### Option 2: ConfigMap-Based Channel Mapping

Create ConfigMap mapping namespaces or applications to Slack channels.

**Pros:**
- Centralized configuration
- No deployment manifest changes

**Cons:**
- Requires configuration management overhead
- Not self-service (DevOps must update ConfigMap)
- Adds indirection (harder to understand routing)
- Requires webhook handler updates when mapping changes

**Verdict:** ❌ Creates DevOps bottleneck

#### Option 3: Label-Based Routing (Chosen)

Read `ol.mit.edu/slack-channel` label from deployment and route accordingly.

**Pros:**
- Fits existing `ol.mit.edu/*` label infrastructure
- Self-service (teams add label to their deployments)
- No configuration management required
- Graceful fallback to default channel
- Minimal code changes (~80 lines in one file)
- 100% backward compatible

**Cons:**
- Requires teams to update deployment manifests
- Slack bot must be invited to target channels

**Verdict:** ✅ Best balance of simplicity and functionality

#### Option 4: Annotation-Based Routing

Use `metadata.annotations["ol.mit.edu/slack-channel"]` instead of label.

**Pros:**
- Annotations allow more flexible values

**Cons:**
- Inconsistent with existing `ol.mit.edu/*` label pattern
- Labels are more visible and standard for filtering
- Would need to add annotation reading to webhook handler

**Verdict:** ❌ Labels are preferred and consistent

## Decision

We will implement **label-based Slack channel routing** using the `ol.mit.edu/slack-channel` label on Kubernetes deployments.

### Implementation Details

1. **Label**: `ol.mit.edu/slack-channel: "team-channel-name"`
2. **Location**: Deployment `metadata.labels`
3. **Fallback**: Default channel if label missing or invalid
4. **Code Changes**: Single file (`webhook_handler.py`, ~80 lines)
5. **Infrastructure Changes**: None (existing webhook handler updated)

### Usage Pattern

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  labels:
    ol.mit.edu/application: my-app
    ol.mit.edu/slack-channel: "team-alpha-alerts"
spec:
  # ... deployment spec
```

### Error Handling

- **Label missing**: Use default channel
- **Label empty**: Use default channel, log warning
- **Channel not found**: Retry with default channel, log warning
- **Slack bot not in channel**: Retry with default channel, log warning

## Consequences

### Positive

1. **Self-Service**: Teams can route their notifications without DevOps intervention
2. **Scalable**: Supports unlimited teams/channels with no additional overhead
3. **Backward Compatible**: Existing deployments continue to work unchanged
4. **Consistent**: Follows existing `ol.mit.edu/*` label patterns
5. **Resilient**: Graceful fallback prevents notification loss
6. **Simple**: Single file change, no new infrastructure
7. **Discoverable**: Labels are visible in `kubectl describe` and K8s dashboards

### Negative

1. **Manual Updates**: Teams must add label to deployment manifests (one-time task)
2. **Channel Management**: Slack bot must be invited to target channels (Slack admin task)
3. **Limited Validation**: No built-in whitelist of allowed channels (can be added later)
4. **Label Sprawl**: Adds another label to deployment metadata (minor)

### Neutral

1. **Requires Documentation**: Teams need to know about the feature (documented in README)
2. **Testing Required**: Integration tests needed across environments (standard practice)
3. **Rollout Phases**: Deploy to CI → QA → Production (standard rollout)

## Alternatives Rejected

- **Multiple Kubewatch Instances**: Too much operational overhead
- **ConfigMap Routing**: Not self-service, creates bottleneck
- **Annotation-Based**: Inconsistent with existing patterns

## Implementation Timeline

1. **Phase 1**: Code changes and validation (3 hours) - ✅ Complete
2. **Phase 2**: Deploy to CI, integration test (1 hour) - ⏸️ Pending
3. **Phase 3**: Deploy to QA and Production (1 hour) - ⏸️ Pending

**Total Effort**: ~5 hours

## Validation Criteria

- [ ] Code passes ruff, mypy checks (✅ Complete)
- [ ] Deployments with label route to specified channel
- [ ] Deployments without label use default channel
- [ ] Invalid channels fall back to default with warning
- [ ] No disruption to existing notifications
- [ ] Documentation updated (✅ Complete)

## Related Decisions

- **ADR-0001**: Use ADR for Architecture Decisions (meta-ADR)
- This decision follows the established pattern of using `ol.mit.edu/*` labels for deployment metadata

## Review History

- **2025-11-20**: Created by AI agent, pending human approval
- Status: **Proposed** (awaiting deployment and testing)

## References

- Implementation: `src/ol_infrastructure/applications/kubewatch_webhook_handler/webhook_handler.py`
- Documentation: `src/ol_infrastructure/applications/kubewatch_webhook_handler/README.md`
- Technical Assessment: [Stored in session artifacts]
- Label Infrastructure: `src/ol_infrastructure/lib/ol_types.py` (K8sGlobalLabels)
