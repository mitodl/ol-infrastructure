# UAI Partners Storage

## Overview

This project enables Universal AI partners to consume data via S3 directly or through an SFTP server. Each partner has isolated access to their own S3 prefix.

## Architecture

### Resources Created
- **S3 Bucket**: Named `ol-uai-partners-storage-{environment}` with versioning enabled
- **SFTP Server**: AWS Transfer Family server with SERVICE_MANAGED identity provider
- **IAM Roles**: Per-partner IAM roles with scoped S3 permissions
- **Partner Directories**: Each partner gets an S3 prefix: `/{bucket}/{username}/`

### Security Model
- Each partner can only access their own prefix via S3 and SFTP
- IAM policies enforce path-based access control
- Cross-account S3 access enabled via partner AWS account ID in IAM role trust policy
- SSH public key authentication required (no passwords)
- Public access to bucket blocked
- Versioning enabled for data recovery

## Configuration

### Adding Partners

Edit the appropriate stack configuration file (`Pulumi.applications.uai_partners_storage.{Environment}.yaml`):

```yaml
config:
  uai_partners:partners:
    - name: partner1
      username: partner1_user
      aws_account_id: "123456789012"
      ssh_public_key: ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQC... partner1@example.com
    - name: partner2
      username: partner2_user
      aws_account_id: "987654321098"
      ssh_public_key: ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQD... partner2@example.com
```

Each partner entry requires:
- `name`: Descriptive name for the partner (used for documentation)
- `username`: SFTP username (used as S3 prefix directory)
- `aws_account_id`: Partner's AWS account ID (12-digit number) for cross-account S3 access
- `ssh_public_key`: SSH public key for authentication

### Removing Partners

Remove the partner entry from the configuration and run `pulumi up`. The SFTP user and IAM role will be deleted, but S3 data is retained.

## Deployment

### Prerequisites
1. Pulumi logged in: `pulumi login s3://mitol-pulumi-state`
2. AWS credentials configured
3. Python dependencies installed: `uv sync`

### Initialize Stack (First Time)
```bash
cd src/ol_infrastructure/applications/uai_partners_storage
pulumi stack init applications.uai_partners_storage.QA
```

### Deploy Changes
```bash
cd src/ol_infrastructure/applications/uai_partners_storage
pulumi stack select applications.uai_partners_storage.QA
pulumi up
```

### View Configuration
```bash
pulumi config
pulumi stack output
```

## Partner Access

### SFTP Connection
Partners connect using:
```bash
sftp -i /path/to/private_key {username}@{sftp_endpoint}
```

Get the SFTP endpoint:
```bash
pulumi stack output sftp_endpoint
```

### S3 Direct Access
Partners with S3 credentials can access:
```
s3://ol-uai-partners-storage-{environment}/{username}/
```

## Acceptance Criteria

✅ Each partner can only access their own prefix in the S3 bucket via S3 and SFTP
✅ Partners are added/removed via configuration (no code changes)
✅ IAM policies enforce isolation between partners
✅ SSH key authentication required

## Implementation Details

This project uses the `SFTPServer` component from `ol_infrastructure.components.aws.sftp`, which creates:
- AWS Transfer Family SFTP server
- S3 bucket with versioning and public access blocking
- Per-user IAM roles with scoped permissions
- SFTP users with SSH key authentication
- Home directory mappings to partner-specific prefixes
