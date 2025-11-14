# Quick Reference - DCC Services

## Service Endpoints (Internal)

- **Issuer Coordinator**: `http://issuer-coordinator.digital-credentials.svc.cluster.local:4005`
- **Signing Service**: `http://signing-service.digital-credentials.svc.cluster.local:4006`

## API Endpoints

### Signing Service
- `GET /` - Health check
- `GET /did-key-generator` - Generate new signing keys

### Issuer Coordinator
- `POST /credentials/issue` - Issue a new credential
- `POST /credentials/status` - Check credential status

## Issue Credential (Python Example)

```python
import requests

# Credential payload
credential = {
    "@context": [
        "https://www.w3.org/2018/credentials/v1",
        "https://purl.imsglobal.org/spec/ob/v3p0/context.json"
    ],
    "type": ["VerifiableCredential", "OpenBadgeCredential"],
    "issuer": {
        "id": "did:key:YOUR_DID_FROM_KEY_GENERATION",
        "name": "MIT Open Learning"
    },
    "credentialSubject": {
        "type": "AchievementSubject",
        "achievement": {
            "name": "Course Name",
            "description": "Course completion description"
        }
    }
}

# Issue credential
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
        "Authorization": "Bearer YOUR_TENANT_TOKEN",
        "Content-Type": "application/json"
    }
)

signed_credential = response.json()
```

## Configuration Values

Add to `Pulumi.<stack>.yaml`:

```yaml
config:
  digital-credentials:signing_service_replicas: "2"
  digital-credentials:issuer_coordinator_replicas: "2"
  digital-credentials:enable_status_service: "false"
```

## Troubleshooting Commands

```bash
# Check pod status
kubectl get pods -n digital-credentials

# View logs
kubectl logs -n digital-credentials deployment/issuer-coordinator
kubectl logs -n digital-credentials deployment/signing-service

# Port forward for testing
kubectl port-forward -n digital-credentials svc/issuer-coordinator 4005:4005
kubectl port-forward -n digital-credentials svc/signing-service 4006:4006

# Test services
curl http://localhost:4005/
curl http://localhost:4006/did-key-generator

# Check secrets
kubectl get secrets -n digital-credentials
kubectl get configmaps -n digital-credentials
```

## Multi-Tenant Setup

Each tenant needs:
1. A signing key seed (from `/did-key-generator`)
2. An API token (for authentication)

Add to secrets:
```yaml
# signing_service.<env>.yaml
tenants:
  TENANT_SEED_DEPARTMENT1: "seed1..."
  TENANT_SEED_DEPARTMENT2: "seed2..."

# issuer_coordinator.<env>.yaml
tenant_tokens:
  TENANT_TOKEN_DEPARTMENT1: "token1..."
  TENANT_TOKEN_DEPARTMENT2: "token2..."
```

When issuing, use:
- The DID from the key generation in `issuer.id`
- The corresponding token in `Authorization` header
