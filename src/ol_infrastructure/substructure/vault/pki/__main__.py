from pulumi import Config, export, ResourceOptions, StackReference

from ol_infrastructure.components.services.vault import (
    OLVaultPKIIntermediateCABackend,
    OLVaultPKIIntermediateCABackendConfig,
    OLVaultPKIIntermediateEnvBackend,
    OLVaultPKIIntermediateEnvBackendConfig,
)
from ol_infrastructure.lib.ol_types import BusinessUnit
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

SIX_MONTHS = 60 * 60 * 24 * 30 * 6
TWELVE_MONTHS = 60 * 60 * 24 * 30 * 12

env_config = Config("environment")
stack_info = parse_stack()
pki_intermediate_export = {}

root_ca = StackReference("infrastcuture.aws.private_ca").require_output("root_ca")
root_ca_arn = root_ca["arn"]

setup_vault_provider()

pki_intermediate_ca_config = OLVaultPKIIntermediateCABackendConfig(
    max_ttl=TWELVE_MONTHS * 5,
    default_ttl=TWELVE_MONTHS * 4,
    acmpca_rootca_arn=root_ca_arn,
)
pki_intermediate_ca = OLVaultPKIIntermediateCABackend(pki_intermediate_ca_config)


for business_unit in BusinessUnit:
    pki_intermediate_env_config = OLVaultPKIIntermediateEnvBackendConfig(
        max_ttl=TWELVE_MONTHS,
        default_ttl=TWELVE_MONTHS,
        environment_name=f"{business_unit.value}",
        acmpca_rootca_arn=root_ca_arn,
        parent_intermediate_ca=pki_intermediate_ca,
        opts=ResourceOptions(parent=pki_intermediate_ca),
    )

    pki_intermediate_env = OLVaultPKIIntermediateEnvBackend(pki_intermediate_env_config)

    pki_intermediate_export.update(
        {
            f"pki_intermediate_{business_unit.value}": pki_intermediate_env,
        }
    )

export("pki_intermediate_export", pki_intermediate_export)
export("pki_intermediate_ca", pki_intermediate_ca)
