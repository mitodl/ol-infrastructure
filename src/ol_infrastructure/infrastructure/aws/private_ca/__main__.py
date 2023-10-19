from pulumi import export
from pulumi_aws import acmpca

from ol_infrastructure.lib.ol_types import AWSBase

aws_config = AWSBase(tags={"OU": "operations", "Environment": "global"})

ol_root_ca = acmpca.CertificateAuthority(
    "mitol-root-certificate-authority",
    certificate_authority_configuration=acmpca.CertificateAuthorityCertificateAuthorityConfigurationArgs(
        key_algorithm="RSA_4096",
        signing_algorithm="SHA512WITHRSA",
        subject=acmpca.CertificateAuthorityCertificateAuthorityConfigurationSubjectArgs(
            country="US",
            state="Massachusetts",
            locality="Cambridge",
            organization="Massachusetts Institute of Technology",
            organizational_unit="Open Learning",
            common_name="ca.odl.mit.edu",
        ),
    ),
    tags=aws_config.tags,
    type="ROOT",
    enabled=True,
)
ol_root_certificate = acmpca.Certificate(
    "mitol-root-certificate-authority-signed-certificate",
    certificate_authority_arn=ol_root_ca.arn,
    certificate_signing_request=ol_root_ca.certificate_signing_request,
    signing_algorithm="SHA512WITHRSA",
    template_arn="arn:aws:acm-pca:::template/RootCACertificate/V1",
    validity=acmpca.CertificateValidityArgs(
        type="YEARS",
        value="10",
    ),
)
ol_root_authority_certificate = acmpca.CertificateAuthorityCertificate(
    "mitol-root-certificate-authority-certificate-assignment",
    certificate_authority_arn=ol_root_ca.arn,
    certificate=ol_root_certificate.certificate,
    certificate_chain=ol_root_certificate.certificate_chain,
)

export(
    "root_ca",
    {
        "arn": ol_root_ca.arn,
        "certificate": ol_root_authority_certificate.certificate,
    },
)
