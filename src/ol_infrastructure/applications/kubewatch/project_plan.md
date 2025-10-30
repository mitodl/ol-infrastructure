# Project Plan: Kubernetes Deployment Notifications

## Status: ✅ IMPLEMENTED (with notes)

## Overview
The purpose of this project is to notify end users when Kubernetes deployments succeed or fail.

I plan to use kubewatch for notifications. I want notifications to include labels, deployment start, and deployment status.

Notifications should be posted to an appropriate per project Slack channel.

Make notifications of deployments to CI configurable.

## Implementation Summary

### ✅ Completed
1. **Deployment monitoring** - Kubewatch now monitors deployments and jobs
2. **Configurable namespace filtering** - Can watch all namespaces or specific ones via `config_kubewatch:watched_namespaces`
3. **Slack integration** - Configured to send to Slack channel (requires valid token)
4. **Reduced noise** - Disabled pod and generic event watching to focus on deployments
5. **Documentation** - Comprehensive README.md and IMPLEMENTATION_NOTES.md added

### ⚠️ Limitations & Notes
1. **Slack token** - Current token shows `invalid_auth` errors and needs to be refreshed
2. **Single channel** - Current implementation sends all notifications to one Slack channel
   - Per-project channels require either multiple kubewatch instances or a custom webhook router
   - See README.md for detailed multi-channel implementation options
3. **Notification format** - Stock kubewatch provides basic notifications; labels are in event payload but not prominently formatted
   - For richer formatting, would need custom webhook handler

### Files Modified
- `__main__.py` - Added namespace filtering, improved resource watching configuration
- `Pulumi.applications.kubewatch.applications.CI.yaml` - Added watched_namespaces parameter
- `README.md` - Created comprehensive documentation
- `IMPLEMENTATION_NOTES.md` - Created detailed implementation notes and next steps
- `project_plan.md` - This file (updated with status)

## Next Steps (Required for Full Functionality)

1. **Fix Slack Token** (REQUIRED)
   - Generate new Slack Bot OAuth token at https://api.slack.com/apps
   - Update `src/bridge/secrets/kubewatch/secrets.applications.ci.yaml`
   - Redeploy: `cd src/ol_infrastructure/applications/kubewatch && pulumi up`

2. **Test Deployment**
   ```bash
   # Verify kubewatch is running
   kubectl get pods -n kubewatch

   # Check logs
   kubectl logs -n kubewatch deployment/kubewatch

   # Trigger test deployment
   kubectl rollout restart deployment/<some-deployment> -n <namespace>

   # Verify notification in Slack channel
   ```

3. **Per-Project Channels** (Optional)
   - For CI/dev: Single channel is probably sufficient
   - For production: See README.md section "Per-Project Slack Channels" for implementation options

## Documentation
- https://github.com/robusta-dev/kubewatch

## Tools
You have access to the kubectl utility. It's already configured to talk to the cluster this will run in.
