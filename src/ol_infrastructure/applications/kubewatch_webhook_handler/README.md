# Kubewatch Webhook Handler

Custom webhook handler for kubewatch that enriches deployment notifications with detailed information and sends rich Slack messages.

## Overview

This Pulumi project builds and deploys a webhook handler service that:
- Receives webhook events from kubewatch
- Queries Kubernetes API for detailed deployment information
- Formats rich Slack messages with Block Kit
- Routes notifications to different Slack channels based on deployment labels
- Posts enhanced notifications to Slack

## What It Provides

### Rich Deployment Notifications

Instead of basic messages:
```
[namespace] deployment name was updated
```

You get detailed notifications with:
- **Deployment Status**: ✅ Success, 🔄 In Progress, ❌ Failed
- **Timestamps**: Creation time and last update time
- **Labels**: All `ol.mit.edu/*` labels displayed
- **Replicas**: Ready count vs desired count
- **Container Image**: Current image being deployed
- **Progress Messages**: Details from Kubernetes conditions

### Dynamic Slack Channel Routing

**NEW:** Route notifications to different Slack channels based on deployment labels!

Add the `ol.mit.edu/slack-channel` label to your deployment to send notifications to a specific channel:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: my-app
  namespace: mitlearn
  labels:
    ol.mit.edu/application: my-app
    ol.mit.edu/slack-channel: "team-alpha-alerts"  # Route to this channel
spec:
  # ... deployment spec
```

**Features:**
- Deployments without the label use the default channel
- Invalid or missing channels automatically fall back to default
- No configuration changes required - just add the label
- Supports both channel names (`#my-channel`) and IDs (`C1234567890`)

**Requirements:**
- Slack bot must be invited to target channels
- Channel names should not include the `#` prefix (automatically handled by Slack)

### Example Slack Message

```
🔄 Deployment Update: mitlearn/mitlearn-app

Status: ✅ Successfully Deployed       Replicas: 2/2 ready
Created: 2025-10-15 14:30:00 UTC      Last Update: 2025-10-30 16:45:23 UTC

Image: .../mitodl/mitlearn-app:latest

Labels:
  • ol.mit.edu/application: mitlearn-app
  • ol.mit.edu/process: webapp
  • ol.mit.edu/slack-channel: team-alpha-alerts

ReplicaSet "mitlearn-app-7b9c7875c9" has successfully progressed.
```

## Project Structure

```
kubewatch_webhook_handler/
├── __init__.py                                          # Python package marker
├── __main__.py                                          # Pulumi program
├── Pulumi.yaml                                          # Project definition
├── Pulumi.applications.kubewatch_webhook_handler.applications.CI.yaml  # Stack config
├── webhook_handler.py                                   # Flask application
├── Dockerfile                                           # Container image
├── requirements.txt                                     # Python dependencies
└── README.md                                            # This file
```

## Components Deployed

1. **ECR Repository** - Stores Docker images
2. **Docker Image** - Built from Dockerfile and pushed to ECR
3. **Kubernetes Secret** - Contains Slack token
4. **Kubernetes Deployment** - Runs the webhook handler (2 replicas)
5. **Kubernetes Service** - Exposes the webhook endpoint

## Configuration

### Stack Configuration

Edit `Pulumi.applications.kubewatch_webhook_handler.applications.CI.yaml`:

```yaml
config:
  vault:address: https://vault-ci.odl.mit.edu
  kubewatch_webhook:watched_namespaces: "ecommerce,learn-ai,mitlearn,mitxonline"
  kubewatch_webhook:ignored_label_patterns: "celery"  # Optional: comma-separated patterns

1. **Image Pattern Filtering**
   - Ignores deployments where the container image name contains any specified pattern
   - Case-insensitive matching
   - Example: `"nginx"` ignores `nginx:1.21`, `myapp-nginx:latest`, `registry.io/nginx-proxy`

2. **Label Pattern Filtering**
   - Ignores deployments where any `ol.mit.edu/*` label value contains any specified pattern
   - Only checks labels starting with `ol.mit.edu/`
   - Case-insensitive matching
   - Example: `"celery"` ignores deployments with `ol.mit.edu/process: celery-worker`

3. **Multiple Patterns**
   - Use comma-separated values to specify multiple patterns
   - Example: `"nginx,redis,memcached"` ignores any deployment matching any of these patterns

**Common Use Cases:**
- Ignore sidecar containers: `ignored_image_patterns: "nginx,envoy,istio-proxy"`
- Ignore worker deployments: `ignored_label_patterns: "celery,worker,background"`
- Combined: Filter both image and label patterns for comprehensive noise reduction

### Secrets

Slack token is stored in SOPS-encrypted secrets:
- Location: `src/bridge/secrets/kubewatch/secrets.applications.ci.yaml`
- Key: `slack-token`

## Deployment

### Prerequisites

1. AWS credentials configured
2. Pulumi access to state backend
3. kubectl access to target EKS cluster
4. Docker installed (for building images)

### Deploy Steps

```bash
# Navigate to project directory
cd /home/feoh/src/mit/ol-infrastructure/src/ol_infrastructure/applications/kubewatch_webhook_handler

# Login to Pulumi
pulumi login s3://mitol-pulumi-state

# Select the stack
pulumi stack select applications.kubewatch_webhook_handler.applications.CI

# Preview changes
pulumi preview

# Deploy
pulumi up
```

This will:
1. Create ECR repository
2. Build Docker image from Dockerfile
3. Push image to ECR
4. Create Kubernetes resources (secret, deployment, service)
5. Deploy webhook handler to kubewatch namespace

### Verify Deployment

```bash
# Check pods are running
kubectl get pods -n kubewatch -l app=kubewatch-webhook

# Check service
kubectl get svc -n kubewatch kubewatch-webhook

# View logs
kubectl logs -n kubewatch -l app=kubewatch-webhook --tail=50

# Test health endpoint
kubectl exec -n kubewatch deployment/kubewatch-webhook -- curl http://localhost:8080/health
```

## Integration with Kubewatch

After deploying the webhook handler, update the main kubewatch configuration to use it:

1. Edit `../kubewatch/__main__.py`
2. Disable direct Slack posting:
   ```python
   "slackwebhook": {
       "enabled": False,
   },
   ```
3. Enable webhook handler:
   ```python
   "webhook": {
       "enabled": True,
       "url": "http://kubewatch-webhook.kubewatch.svc.cluster.local/webhook/kubewatch",
   },
   ```
4. Deploy kubewatch updates:
   ```bash
   cd ../kubewatch
   pulumi up
   ```

## Testing

### Trigger a Test Deployment

```bash
# Restart a deployment to trigger notification
kubectl rollout restart deployment/mitlearn-app -n mitlearn

# Watch webhook handler logs
kubectl logs -n kubewatch -l app=kubewatch-webhook -f

# Check Slack for rich notification
```

### Test the Health Endpoint

```bash
kubectl exec -n kubewatch deployment/kubewatch-webhook -- \
  curl -s http://localhost:8080/health
# Should return: {"status":"healthy"}
```

### Send Test Webhook Event

```bash
kubectl exec -n kubewatch deployment/kubewatch-webhook -- \
  curl -X POST http://localhost:8080/webhook/kubewatch \
  -H "Content-Type: application/json" \
  -d '{
    "eventType": "update",
    "kind": "Deployment",
    "name": "test-deployment",
    "namespace": "mitlearn"
  }'
```

## Monitoring

### View Logs

```bash
# All logs
kubectl logs -n kubewatch -l app=kubewatch-webhook

# Follow logs
kubectl logs -n kubewatch -l app=kubewatch-webhook -f

# Logs from specific pod
kubectl logs -n kubewatch kubewatch-webhook-<pod-id>
```

### Check Pod Status

```bash
# Pod status
kubectl get pods -n kubewatch -l app=kubewatch-webhook

# Describe pod
kubectl describe pod -n kubewatch -l app=kubewatch-webhook

# Check events
kubectl get events -n kubewatch --field-selector involvedObject.name=kubewatch-webhook
```

## Troubleshooting

### Image Build Failures

```bash
# Check Docker is running
docker ps

# Manually build to test
cd /home/feoh/src/mit/ol-infrastructure/src/ol_infrastructure/applications/kubewatch_webhook_handler
docker build -t test-webhook .

# Check ECR authentication
aws ecr get-login-password --region us-east-1
```

### Pod Not Starting

```bash
# Check pod status
kubectl describe pod -n kubewatch -l app=kubewatch-webhook

# Common issues:
# - Image pull errors: Check ECR permissions
# - Secret not found: Ensure kubewatch-webhook-secret exists
# - ServiceAccount: Ensure 'kubewatch' ServiceAccount exists
```

### No Notifications Received

```bash
# 1. Check webhook handler is receiving events
kubectl logs -n kubewatch -l app=kubewatch-webhook | grep "Received event"

# 2. Check kubewatch is sending to webhook
kubectl logs -n kubewatch deployment/kubewatch | grep -i webhook

# 3. Test Slack webhook URL
kubectl get secret -n kubewatch kubewatch-webhook-secret -o jsonpath='{.data.slack-webhook-url}' | base64 -d
# Copy URL and test with curl

# 4. Check namespace filtering
kubectl logs -n kubewatch -l app=kubewatch-webhook | grep "Ignoring event"
```

## Updating

### Update Code

1. Modify `webhook_handler.py`, `Dockerfile`, or `requirements.txt`
2. Run `pulumi up`
3. Pulumi will rebuild the image and redeploy

### Update Configuration

1. Edit stack config file
2. Run `pulumi up`
3. Deployment will be updated with new environment variables

### Force Image Rebuild

```bash
# Tag image with new version
pulumi up --refresh

# Or manually rebuild
docker build -t kubewatch-webhook-handler:$(date +%s) .
```

## Outputs

After deployment, Pulumi exports:
- `ecr_repository_url` - ECR repository URL
- `webhook_image` - Full image name with tag
- `webhook_service_url` - Internal Kubernetes service URL
- `watched_namespaces` - List of namespaces being monitored

View outputs:
```bash
pulumi stack output
```

## Cleanup

To remove the webhook handler:

```bash
pulumi destroy
```

Note: This will NOT delete the ECR repository by default (to preserve images). To delete ECR repository, update the code or delete manually.

## Related Documentation

- Main kubewatch project: `../kubewatch/README.md`
- Webhook handler guide: `../kubewatch/WEBHOOK_HANDLER_GUIDE.md`
- Filtering guide: `../kubewatch/FILTERING_GUIDE.md`
