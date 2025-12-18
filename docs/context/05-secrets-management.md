# Secrets Management (SOPS + Vault)

## Workflow Overview

1. Secrets stored in git as SOPS-encrypted YAML files (`src/bridge/secrets/<context>/`)
2. SOPS config (`.sops.yaml`) defines KMS keys by environment (QA/Production/CI)
3. During `pulumi up`, `bridge.secrets.sops.set_env_secrets()` decrypts secrets in-memory
4. Pulumi writes secrets to HashiCorp Vault
5. Applications read secrets from Vault at runtime

## Required Tools

- `sops` CLI (v3.11.0+)
- AWS credentials for KMS access
- HashiCorp Vault CLI (for manual secret management)

## SOPS Configuration

The `.sops.yaml` file defines which KMS keys are used for different environments:

```yaml
creation_rules:
  - path_regex: src/bridge/secrets/qa/.*
    kms: arn:aws:kms:us-east-1:...  # QA key
  - path_regex: src/bridge/secrets/production/.*
    kms: arn:aws:kms:us-east-1:...  # Production key
```

## Working with Encrypted Secrets

```bash
# View decrypted content (requires KMS access)
sops src/bridge/secrets/qa/app_secrets.yaml

# Edit secrets (opens in editor, encrypts on save)
sops -i src/bridge/secrets/qa/app_secrets.yaml

# Encrypt a new file
sops -e -i src/bridge/secrets/qa/new_secrets.yaml

# Rotate encryption keys (requires KMS access)
sops -e -i src/bridge/secrets/qa/*.yaml
```

## Pulumi Integration

### Setting Secrets in Pulumi Code

```python
from bridge.secrets.sops import set_env_secrets

# During stack initialization
set_env_secrets("qa")  # Decrypts and sets environment variables

# Access secrets in code
import os
db_password = os.environ["DB_PASSWORD"]

# Write to Vault
vault_config = {
    "db_password": db_password,
    "api_key": os.environ["API_KEY"],
}
vault.write_secret(vault_config)
```

### Secret File Organization

```
src/bridge/secrets/
├── qa/
│   ├── app_secrets.yaml       # App-specific secrets
│   ├── database_secrets.yaml  # Database credentials
│   └── api_keys.yaml          # Third-party API keys
├── production/
│   ├── app_secrets.yaml
│   ├── database_secrets.yaml
│   └── api_keys.yaml
└── ci/
    └── deployment_secrets.yaml  # CI/CD credentials
```

## Best Practices

1. **Never commit unencrypted secrets:** All `.yaml` files under `src/bridge/secrets/` must be encrypted
2. **Use SOPS for all secrets:** Don't create custom encryption mechanisms
3. **Group related secrets:** Organize by application or purpose
4. **Rotate regularly:** Update KMS keys periodically (quarterly recommended)
5. **Audit access:** Track who accesses secrets through Vault audit logs
6. **Environment parity:** Keep similar secrets in QA and Production (except values)

## Troubleshooting

### Issue: "KMS access denied" when running `sops`
**Solution:** Ensure AWS credentials are configured and you have KMS decrypt permissions for the target key

### Issue: Secret not available in Pulumi code
**Solution:** Verify `set_env_secrets()` was called before accessing `os.environ`

### Issue: Cannot edit encrypted file
**Solution:** Run `sops -e -i <file>` to ensure SOPS encryption is properly configured

## Creating New Secrets

1. Create a new YAML file: `src/bridge/secrets/<env>/<secret_name>.yaml`
2. Add secrets in YAML format
3. Encrypt: `sops -e -i src/bridge/secrets/<env>/<secret_name>.yaml`
4. Verify encryption: `sops src/bridge/secrets/<env>/<secret_name>.yaml` (should show decrypted content)
5. Commit encrypted file to git

## Vault Documentation

For runtime access to secrets, applications use HashiCorp Vault. See:
- Vault server setup: `src/ol_infrastructure/infrastructure/vault/`
- Application authentication: Vault AppRole or JWT methods
- Secret retrieval: Application-specific Vault client libraries
