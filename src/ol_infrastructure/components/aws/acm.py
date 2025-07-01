from functools import partial

from pulumi import ComponentResource, Output, ResourceOptions
from pulumi_aws import acm
from pydantic import BaseModel, ConfigDict

from ol_infrastructure.lib.aws.route53_helper import (
    acm_certificate_validation_records,
    lookup_zone_id_from_domain,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack


class ACMCertificateConfig(BaseModel):
    certificate_domain: str
    alternative_names: list[str] | None = None
    certificate_zone_id: Output[str] | str | None = None
    certificate_tags: dict[str, str]

    model_config = ConfigDict(arbitrary_types_allowed=True)


class ACMCertificate(ComponentResource):
    def __init__(
        self,
        name: str,
        cert_config: ACMCertificateConfig,
        opts: ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure:aws:acm:ACMCertificate",
            name,
            None,
            opts,
        )

        cert_opts = ResourceOptions(parent=self).merge(opts)

        stack_info = parse_stack()

        acm_cert = acm.Certificate(
            f"{name}-acm-certificate",
            domain_name=cert_config.certificate_domain,
            subject_alternative_names=cert_config.alternative_names,
            validation_method="DNS",
            tags=cert_config.certificate_tags,
            opts=cert_opts,
        )

        self.arn = acm_cert.arn
        self.domain_name = acm_cert.domain_name

        if not cert_config.certificate_zone_id:
            zone_id = lookup_zone_id_from_domain(cert_config.certificate_domain)
            cert_config.certificate_zone_id = zone_id

        acm_cert_validation_records = acm_cert.domain_validation_options.apply(
            partial(
                acm_certificate_validation_records,
                cert_name=name,
                zone_id=cert_config.certificate_zone_id,
                stack_info=stack_info,
                opts=cert_opts,
            )
        )

        acm_validated_cert = acm.CertificateValidation(
            f"wait-for-{name}-acm-cert-validation",
            certificate_arn=acm_cert.arn,
            validation_record_fqdns=acm_cert_validation_records.apply(
                lambda validation_records: [
                    validation_record.fqdn for validation_record in validation_records
                ]
            ),
            opts=cert_opts,
        )

        self.certificate = acm_cert
        self.validated_certificate = acm_validated_cert
