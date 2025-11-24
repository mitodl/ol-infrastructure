from typing import Literal

import pulumi
import pulumi_kubernetes as kubernetes
from pydantic import BaseModel


class OLCertManagerCertConfig(BaseModel):
    application_name: str
    k8s_namespace: str
    k8s_labels: dict[str, str] = {}
    resource_suffix: str = "cert"

    create_apisixtls_resource: bool = False
    apisixtls_ingress_class: str = "apache-apisix"
    dest_secret_name: str
    dns_names: list[str]
    letsencrypt_env: Literal["staging", "production"] = "production"
    usages: list[
        Literal[
            "any",
            "crl sign",
            "cert sign",
            "client auth",
            "code signing",
            "content commitment",
            "data encipherment",
            "decipher only",
            "digital signature",
            "email protection",
            "encipher only",
            "ipsec end system",
            "ipsec tunnel",
            "ipsec user",
            "key agreement",
            "key encipherment",
            "microsoft sgc",
            "netscape sgc",
            "ocsp signing",
            "s/mime",
            "server auth",
            "signing",
            "timestamping",
        ]
    ] = ["digital signature", "key encipherment", "server auth"]


class OLCertManagerCert(pulumi.ComponentResource):
    """
    OLCertManagerCert is a component that creates a certificate using cert-manager.
    """

    def __init__(
        self,
        name: str,
        cert_config: OLCertManagerCertConfig,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__(
            "ol:infrastructure.services.cert_manager:OLCertManagerCert",
            name,
            None,
            opts,
        )

        resource_options = pulumi.ResourceOptions(parent=self)
        self.resource_name = (
            f"{cert_config.application_name}-{cert_config.resource_suffix}"
        )

        # This will (eventually, once the request is processed by letsencrypt...)
        # create a secret containing two keys `tls.key` and `tls.crt`.
        #
        # According to the apisix code this should work fine, despite the
        # documentation saying only they keys `cert` and `key` are supported.
        #
        # Ref: https://github.com/apache/apisix-ingress-controller/blob/adc70f3de2e745a29306fc155721a639a6367b6d/pkg/providers/translation/util.go#L35
        # Ref: https://cert-manager.io/docs/usage/certificate/
        # Ref: https://cert-manager.io/docs/reference/api-docs/#cert-manager.io/v1.Certificate
        self.certificate_resource = kubernetes.apiextensions.CustomResource(
            f"ol-cert-manager-certificate-{self.resource_name}",
            api_version="cert-manager.io/v1",
            kind="Certificate",
            metadata={
                "name": self.resource_name,
                "namespace": cert_config.k8s_namespace,
                "labels": cert_config.k8s_labels,
            },
            spec={
                "issuerRef": {
                    "group": "cert-manager.io",
                    "name": f"letsencrypt-{cert_config.letsencrypt_env}",
                    "kind": "ClusterIssuer",
                },
                "secretName": cert_config.dest_secret_name,
                "dnsNames": cert_config.dns_names,
                "usages": cert_config.usages,
            },
            opts=resource_options,
        )

        if cert_config.create_apisixtls_resource:
            # Ref: https://apisix.apache.org/docs/ingress-controller/concepts/apisix_tls/
            # Ref: https://apisix.apache.org/docs/ingress-controller/references/apisix_tls_v2/
            # Note: ingressClassName is required for the Apache APISix helm chart
            # when using standalone mode with GatewayProxy provider
            self.apisix_tls_resource = kubernetes.apiextensions.CustomResource(
                f"ol-cert-manager-apisix-tls-{self.resource_name}",
                api_version="apisix.apache.org/v2",
                kind="ApisixTls",
                metadata={
                    "name": self.resource_name,
                    "namespace": cert_config.k8s_namespace,
                    "labels": cert_config.k8s_labels,
                },
                spec={
                    "hosts": cert_config.dns_names,
                    "ingressClassName": cert_config.apisixtls_ingress_class,
                    "secret": {
                        "name": cert_config.dest_secret_name,
                        "namespace": cert_config.k8s_namespace,
                    },
                },
                opts=resource_options,
            )
