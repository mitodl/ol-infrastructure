# Digital Credentials Consortium (DCC) Services Deployment

This directory contains Pulumi infrastructure code for deploying the Digital Credentials Consortium (DCC) services for issuing verifiable credentials.

## Architecture

The deployment includes two core services:

### 1. Signing Service
- **Image**: `digitalcredentials/signing-service:1.0.0`
- **Port**: 4006
- **Purpose**: Cryptographic signing of credentials using Ed25519 keys
- **Features**:
  - Stateless service
  - Multi-tenant support via environment variables
  - DID key generation endpoint

### 2. Issuer Coordinator
- **Image**: `digitalcredentials/issuer-coordinator:1.0.0`
- **Port**: 4005
- **Purpose**: Orchestrates credential signing and coordinates with status services
- **Features**:
  - REST API with `/credentials/issue` and `/credentials/status` endpoints
  - Multi-tenancy for different signing keys
  - Status service integration (optional)

## Prerequisites

1. **AWS Credentials**: Configured for the target environment
2. **Pulumi**: Logged in to state backend (`pulumi login s3://mitol-pulumi-state`)
3. **SOPS**: For encrypting/decrypting secrets
4. **kubectl**: For verifying deployments (optional)

## Initial Setup

### 1. Generate Signing Keys

For each tenant (issuer), generate signing keys:

```bash
# Deploy signing service first (without tenant config)
pulumi up

# Port-forward to the signing service
kubectl port-forward -n digital-credentials svc/signing-service 4006:4006

# Generate keys
curl http://localhost:4006/did-key-generator

# Save the output - you'll need:
# - secretKeySeed (for signing service)
# - id (DID identifier for credential issuance)
```

### 2. Configure Secrets

Create secret files from templates:

```bash
cd src/bridge/secrets/digital_credentials/

# Copy templates
cp signing_service.qa.yaml.template signing_service.qa.yaml
cp issuer_coordinator.qa.yaml.template issuer_coordinator.qa.yaml

# Edit files and add generated keys/tokens
vim signing_service.qa.yaml
vim issuer_coordinator.qa.yaml

# Encrypt with SOPS
sops -e -i signing_service.qa.yaml
sops -e -i issuer_coordinator.qa.yaml

# Commit encrypted files
git add signing_service.qa.yaml issuer_coordinator.qa.yaml
git commit -m "Add DCC service secrets for QA"
```

### 3. Deploy Services

```bash
cd src/ol_infrastructure/applications/digital_credentials/

# Select stack
pulumi stack select applications.digital-credentials.QA

# Preview changes
pulumi preview

# Deploy
pulumi up
```

## Configuration

Stack configuration options (set via `pulumi config` or stack YAML files):

```yaml
config:
  digital-credentials:
    # Signing service configuration
    signing_service_image: digitalcredentials/signing-service:1.0.0
    signing_service_replicas: 2

    # Issuer coordinator configuration
    issuer_coordinator_image: digitalcredentials/issuer-coordinator:1.0.0
    issuer_coordinator_replicas: 2

    # Status service (optional)
    enable_status_service: "false"
```

## Usage Examples

### Issue a Credential

From your Django application or any HTTP client:

```python
import requests

# Prepare credential data
credential = {
    "@context": [
        "https://www.w3.org/2018/credentials/v1",
        "https://purl.imsglobal.org/spec/ob/v3p0/context.json"
    ],
    "type": ["VerifiableCredential", "OpenBadgeCredential"],
    "issuer": {
        "id": "did:key:...",  # Your DID from key generation
        "name": "MIT Open Learning"
    },
    "credentialSubject": {
        "type": "AchievementSubject",
        "achievement": {
            "name": "Course Completion",
            "description": "Completed Python Programming course"
        }
    }
}

# Call issuer coordinator (internal service)
response = requests.post(
    "http://issuer-coordinator.digital-credentials.svc.cluster.local:4005/credentials/issue",
    json={
        "credential": credential,
        "options": {
            "credentialStatus": {
                "type": "StatusList2021Entry"
            }
        }
    },
    headers={
        "Authorization": f"Bearer {TENANT_TOKEN}"
    }
)

signed_credential = response.json()
```

### Verify Credential Status

```bash
# Check if a credential has been revoked/suspended
curl http://issuer-coordinator:4005/credentials/status \
  -H "Authorization: Bearer YOUR_TENANT_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"credentialId": "urn:uuid:credential-id", "credentialStatus": [...]}'
```

## Multi-Tenancy

Support multiple issuers (departments, schools) by adding tenant configurations:

1. **Generate keys for each tenant** using the signing service
2. **Add tenant secrets** to secret files:
   ```yaml
   # signing_service.qa.yaml
   tenants:
     TENANT_SEED_DEFAULT: "seed1..."
     TENANT_SEED_CE: "seed2..."  # Continuing Education
     TENANT_SEED_RESEARCH: "seed3..."

   # issuer_coordinator.qa.yaml
   tenant_tokens:
     TENANT_TOKEN_DEFAULT: "token1..."
     TENANT_TOKEN_CE: "token2..."
     TENANT_TOKEN_RESEARCH: "token3..."
   ```
3. **Reference tenant in API calls** using appropriate token

## Monitoring

### Check Service Health

```bash
# List pods
kubectl get pods -n digital-credentials

# Check logs
kubectl logs -n digital-credentials deployment/signing-service
kubectl logs -n digital-credentials deployment/issuer-coordinator

# Port forward for testing
kubectl port-forward -n digital-credentials svc/issuer-coordinator 4005:4005
```

### Metrics & Alerts

Services expose health endpoints:
- Signing Service: `http://signing-service:4006/`
- Issuer Coordinator: `http://issuer-coordinator:4005/`

## Troubleshooting

### Credential Issuance Fails

1. **Check signing service connectivity**:
   ```bash
   kubectl exec -n digital-credentials deployment/issuer-coordinator -- \
     curl http://signing-service:4006/
   ```

2. **Verify tenant configuration**: Ensure `TENANT_SEED_*` in signing service matches DID in credential issuer field

3. **Check authentication**: Verify `TENANT_TOKEN_*` is correct

### Services Not Starting

1. **Check secret creation**:
   ```bash
   kubectl get secrets -n digital-credentials
   kubectl describe secret issuer-coordinator-secret -n digital-credentials
   ```

2. **Review pod events**:
   ```bash
   kubectl describe pod -n digital-credentials -l app=issuer-coordinator
   ```

## Security Considerations

1. **Secret Key Protection**: Never commit unencrypted secret seeds
2. **Token Rotation**: Regularly rotate tenant tokens
3. **Network Policies**: Services are ClusterIP only - not exposed externally
4. **RBAC**: Limit access to namespace and secrets
5. **Audit Logging**: Monitor credential issuance via application logs

## References

- [DCC Issuer Coordinator](https://github.com/digitalcredentials/issuer-coordinator)
- [DCC Signing Service](https://github.com/digitalcredentials/signing-service)
- [W3C Verifiable Credentials](https://www.w3.org/TR/vc-data-model-2.0/)
- [DID Key Method](https://w3c-ccg.github.io/did-method-key/)
- [Deployment Analysis](../../../../../../dcc_deployment_analysis.md)

## Next Steps

1. **Status Service**: Add MongoDB and deploy status-service-db for revocation support
2. **Ingress**: Configure external access if needed (with authentication)
3. **Integration**: Connect with Django application for credential issuance
4. **Monitoring**: Set up Prometheus metrics and Grafana dashboards
5. **Backup**: Implement backup strategy for signing keys (via Vault)
