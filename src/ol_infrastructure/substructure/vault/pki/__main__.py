from pulumi import Config, ResourceOptions, StackReference, export

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

root_ca = StackReference("infrastructure.aws.private_ca").require_output("root_ca")
root_ca_arn = root_ca["arn"]

setup_vault_provider(stack_info)

pki_intermediate_ca_config = OLVaultPKIIntermediateCABackendConfig(
    max_ttl=TWELVE_MONTHS * 5,
    default_ttl=TWELVE_MONTHS * 4,
    acmpca_rootca_arn=root_ca_arn,
)
pki_intermediate_ca = OLVaultPKIIntermediateCABackend(
    backend_config=pki_intermediate_ca_config,
)

pki_intermediate_ca_export_struct = {
    "intermediate_certificate": (
        pki_intermediate_ca.pki_intermediate_ca_aws_signed_csr.certificate
    ),
    "intermediate_certificate_bundle": (
        pki_intermediate_ca.pki_intermediate_ca_aws_signed_csr.certificate_chain
    ),
    "root_ca_arn": (
        pki_intermediate_ca.pki_intermediate_ca_aws_signed_csr.certificate_authority_arn
    ),
    "intermediate_crl_url": (
        pki_intermediate_ca.pki_intermediate_ca_config_urls.crl_distribution_points[0]
    ),
    "intermediate_issuing_url": (
        pki_intermediate_ca.pki_intermediate_ca_config_urls.issuing_certificates[0]
    ),
}

for business_unit in BusinessUnit:
    pki_intermediate_env_config = OLVaultPKIIntermediateEnvBackendConfig(
        max_ttl=TWELVE_MONTHS * 2,
        default_ttl=TWELVE_MONTHS,
        environment_name=business_unit.value,
        acmpca_rootca_arn=root_ca_arn,
        parent_intermediate_ca=pki_intermediate_ca,
    )

    pki_intermediate_env = OLVaultPKIIntermediateEnvBackend(
        backend_config=pki_intermediate_env_config,
        opts=ResourceOptions(depends_on=[pki_intermediate_ca]),
    )

    pki_intermediate_env_export_struct = {
        "mount_path": pki_intermediate_env.pki_intermediate_environment_backend.path,
        "intermediate_certificate": (
            pki_intermediate_env.pki_intermediate_environment_signed_csr.certificate
        ),
        "intermediate_certificate_bundle": (
            pki_intermediate_env.pki_intermediate_environment_signed_csr.certificate_bundle
        ),
        "intermediate_certificate_ca_chains": (
            pki_intermediate_env.pki_intermediate_environment_signed_csr.ca_chains
        ),
        "intermediate_certificate_issuing_ca": (
            pki_intermediate_env.pki_intermediate_environment_signed_csr.issuing_ca
        ),
        "intermediate_common_name": (
            pki_intermediate_env.pki_intermediate_environment_signed_csr.common_name
        ),
        "intermediate_serial_number": (
            pki_intermediate_env.pki_intermediate_environment_signed_csr.serial_number
        ),
        "intermediate_crl_url": pki_intermediate_env.pki_intermediate_environment_config_urls.crl_distribution_points[
            0
        ],
        "intermediate_issuing_url": pki_intermediate_env.pki_intermediate_environment_config_urls.issuing_certificates[
            0
        ],
    }

    export(
        f"pki_intermediate_{business_unit.value}", pki_intermediate_env_export_struct
    )

export("pki_intermediate_ca", pki_intermediate_ca_export_struct)
