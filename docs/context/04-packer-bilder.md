# Packer AMI Building

Packer images in `src/bilder/images/` use HCL definitions and PyInfra for provisioning.

## Build Workflow

**Build workflow (not typically run locally):**
1. Packer reads `.pkr.hcl` files (variables, builder config)
2. Launches EC2 instance from base AMI
3. Runs `deploy.py` using PyInfra to configure instance
4. Creates AMI snapshot

## Directory Structure

```
src/bilder/
├── components/               # Reusable provisioning components
│   ├── hashicorp/           # Consul, Vault installers
│   ├── vector/              # Vector log agent
│   └── baseline/            # Base system setup
├── images/                  # AMI definitions
│   ├── consul/              # Example: Consul server AMI
│   │   ├── deploy.py        # PyInfra provisioning script
│   │   ├── files/           # Static files
│   │   ├── templates/       # Config templates
│   │   └── consul.pkr.hcl   # Packer definition
│   └── ...                  # Each dir is a Packer build
```

## Common Operations

```bash
# Format Packer files
packer fmt src/bilder/images/

# Validate a Packer template (requires AWS credentials)
cd src/bilder/images/consul/
packer validate .
```

## PyInfra Provisioning

**What is PyInfra?** Pure Python scripts that define system state (install packages, configure services, etc.).

Example `deploy.py` pattern:
```python
from pyinfra import host
from pyinfra.operations import apt, files, systemd

# Install packages
apt.packages(
    packages=["consul", "unzip"],
    update=True,
)

# Deploy config files
files.put(
    src="templates/consul.hcl",
    dest="/etc/consul.d/consul.hcl",
)

# Enable and start service
systemd.service(
    service="consul",
    enabled=True,
    running=True,
)
```

See `src/bilder/images/consul/deploy.py` for a complete example.

## Code Style & Conventions

### Packer
- **HCL formatting:** Always run `packer fmt -recursive src/bilder/` before committing
- **Components:** Reuse components from `src/bilder/components/` instead of duplicating logic
- **Variables:** Use Packer variables for configuration (avoid hardcoding values)
- **Naming:** Use descriptive names for builders and provisioners

## Best Practices

1. **Reuse components:** Create shared provisioning components for common tasks (baseline setup, monitoring agents, etc.)
2. **Test locally:** Use `packer validate` before committing HCL changes
3. **Version pinning:** Specify package versions in provisioning scripts to ensure consistency
4. **Idempotence:** PyInfra operations should be idempotent (safe to run multiple times)
5. **Documentation:** Document custom components and provisioning logic in README files

## Troubleshooting

- **EC2 instance fails to launch:** Check AWS credentials and IAM permissions
- **Provisioning hangs:** Check EC2 security group rules (ensure SSH access)
- **AMI not created:** Check logs in Packer output for provisioning errors
- **HCL syntax errors:** Run `packer fmt -recursive src/bilder/` and `packer validate`
