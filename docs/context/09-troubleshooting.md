# Troubleshooting Common Issues

## Build & Dependency Issues

### Issue: README mentions Poetry but `poetry.lock` doesn't exist
**Solution:** Repository has migrated to `uv`. Always use `uv sync` and `uv run <command>`.

```bash
uv sync  # Install dependencies
uv run ruff check src/  # Run commands with uv
```

### Issue: Dependencies fail to install
**Solution:** Ensure you're running the correct Python version and uv is updated

```bash
python --version  # Should be 3.12.x
uv --version      # Should be 0.9.3 or higher
uv sync --upgrade  # Update dependencies
```

### Issue: `uv` warns about `~=3.12` in `requires-python`
**Solution:** This is a warning, not an error. Ignore it—fixing requires changing project config.

## Linting & Type Checking

### Issue: `mypy` reports 1316 errors
**Solution:** Expected. Only fix new errors introduced by your changes. Many legacy type issues exist.

**To see only NEW errors in your changes:**
```bash
# Before making changes
mypy src/ > baseline.txt

# After making changes
mypy src/ | diff - baseline.txt
```

### Issue: `ruff check` reports 809 errors
**Solution:** Expected. Focus on not introducing NEW errors. Consider using `--fix` for auto-fixable issues.

**Auto-fix common issues:**
```bash
uv run ruff check --fix src/
```

### Issue: pre-commit hook `hadolint-docker` fails with existing Docker warnings
**Solution:** Ignore hadolint failures if your changes don't affect Dockerfiles. These are pre-existing issues.

To skip hadolint during development:
```bash
# Skip pre-commit hooks
git commit --no-verify
```

### Issue: Code format changed unexpectedly
**Solution:** Run `ruff format` before committing

```bash
uv run ruff format src/
uv run ruff check src/
```

## Pulumi Issues

### Issue: Pulumi operation requires AWS credentials
**Solution:** Ensure AWS CLI is configured

```bash
# Check if configured
aws sts get-caller-identity

# Configure if needed
aws configure

# Or use environment variables
export AWS_ACCESS_KEY_ID="..."
export AWS_SECRET_ACCESS_KEY="..."
```

### Issue: "Error: Failed to authenticate with Pulumi Cloud"
**Solution:** Login to Pulumi state backend

```bash
pulumi login s3://mitol-pulumi-state
```

### Issue: Stack already exists
**Solution:** Select existing stack instead of creating new one

```bash
pulumi stack ls          # List available stacks
pulumi stack select <stack_name>  # Select existing stack
```

### Issue: `pulumi preview` hangs or times out
**Solution:** Check for resource creation bottlenecks

```bash
# Preview with verbose output
pulumi preview -v 2>&1 | head -100

# Cancel if needed
Ctrl+C
```

### Issue: Resource already exists in AWS
**Solution:** Import existing resource or use different name

```bash
# Option 1: Import existing resource
pulumi import <type> <name> <id>

# Option 2: Use different name in Pulumi
# Change resource name in __main__.py
```

## Packer Issues

### Issue: Packer validation fails with HCL syntax error
**Solution:** Format Packer files

```bash
packer fmt -recursive src/bilder/
packer validate src/bilder/images/consul/
```

### Issue: EC2 instance fails to launch
**Solution:** Check AWS credentials, IAM permissions, and security groups

```bash
# Verify AWS credentials
aws ec2 describe-instances

# Check IAM permissions for EC2, VPC
aws iam get-user

# Verify security group allows SSH (usually from VPC or bastion)
aws ec2 describe-security-groups
```

### Issue: Provisioning hangs
**Solution:** Check EC2 security group and network settings

```bash
# Ensure SSH access is enabled
aws ec2 describe-security-groups --group-ids <sg-id>

# Check instance logs
aws ec2 get-console-output --instance-id <i-id>
```

### Issue: AMI not created
**Solution:** Check provisioning logs for errors

```bash
# Look for provisioning errors in Packer output
# Check instance user data logs on EC2
tail -f /var/log/cloud-init-output.log
```

## Secrets Management Issues

### Issue: "KMS access denied" when running `sops`
**Solution:** Ensure AWS credentials are configured and you have KMS decrypt permissions

```bash
# Verify you can access KMS key
aws kms describe-key --key-id <kms-key-id>

# Check IAM permissions
aws iam get-user-policy --user-name <user> --policy-name <policy>
```

### Issue: Secret not available in Pulumi code
**Solution:** Verify `set_env_secrets()` was called before accessing secrets

```python
from bridge.secrets.sops import set_env_secrets
import os

# MUST call set_env_secrets() before accessing os.environ
set_env_secrets("qa")

# Now secrets are available
password = os.environ["DB_PASSWORD"]  # Works
```

### Issue: Cannot edit encrypted file
**Solution:** Run `sops -e -i <file>` to ensure SOPS encryption is properly configured

```bash
# Verify SOPS configuration
cat .sops.yaml

# Edit with SOPS
sops -i src/bridge/secrets/qa/app_secrets.yaml

# Verify encryption (should show decrypted content temporarily)
sops src/bridge/secrets/qa/app_secrets.yaml
```

## Testing Issues

### Issue: Pytest test discovery fails
**Solution:** Ensure test files are named `test_*.py` and located in `tests/` directory

```bash
# Verify structure
ls tests/
ls tests/unit/components/

# Check if test runs
uv run pytest tests/ -v
```

### Issue: "RuntimeError: asyncio event loop" in Pulumi tests
**Solution:** Add event loop initialization at top of test file

```python
import asyncio

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())
```

### Issue: Pulumi mocks not being used
**Solution:** Set mocks BEFORE importing code under test

```python
import pulumi

# MUST set mocks before import
class MyMocks(pulumi.runtime.Mocks):
    def new_resource(self, args):
        return [args.name + "_id", args.inputs]
    def call(self, args):
        return {}

pulumi.runtime.set_mocks(MyMocks())

# NOW import code under test
from ol_infrastructure.components.aws.my_component import MyComponent
```

### Issue: Integration test times out
**Solution:** Increase pytest timeout or check for resource creation issues

```bash
# Increase timeout
pytest tests/integration/ -v --timeout=300

# Check if resources are being created
pulumi stack output --json
```

## Timeout Issues

### Issue: Commands exceed timeout threshold
**Solution:** Increase timeout limits depending on operation

```bash
# mypy takes 75+ seconds
uv run mypy src/  # Budget 120s

# Pre-commit may take 2+ minutes
uv run pre-commit run --all-files  # Budget 180s

# Integration tests can take 5+ minutes
uv run pytest tests/integration/ -v  # Budget 300s
```

## Debugging Tips

### Enable verbose logging
```bash
# Pulumi verbose output
pulumi preview -v

# Python debug logging
export PYTHONVERBOSE=2
uv run mypy src/

# Packer verbose output
PACKER_LOG=1 packer build .
```

### Check git status
```bash
# Ensure changes are staged
git status
git diff

# Verify formatting before commit
uv run ruff format src/
git diff  # Should show no changes
```

### Inspect resource properties
```python
# In Pulumi code, export resource details for inspection
import pulumi

resource = aws.ec2.SecurityGroup(...)
pulumi.export("security_group_id", resource.id)
pulumi.export("security_group_tags", resource.tags)

# View outputs after deploy
pulumi stack output --json
```

## Getting Help

1. **Check repository history:** `git log -p -- <file>` to see how similar code was implemented
2. **Review examples:** Look at existing components in `src/ol_infrastructure/components/`
3. **Test incrementally:** Make small changes and validate frequently
4. **Enable debugging:** Use verbose flags and print debugging output
5. **Check Pulumi docs:** https://www.pulumi.com/docs/
6. **Review test examples:** `tests/ol_infrastructure/components/aws/test_kubernetes_app_auth.py`
