# Create the resources needed to run an edxnotes server

import base64
import json
import os
import textwrap
from pathlib import Path

import pulumi_consul as consul
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
import yaml
from pulumi import Config, Output, ResourceOptions, StackReference, export
from pulumi_aws import ec2, get_caller_identity, iam, route53

from bridge.lib.magic_numbers import DEFAULT_HTTPS_PORT
from bridge.secrets.sops import read_yaml_secrets
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.components.applications.eks import (
    OLEKSAuthBinding,
    OLEKSAuthBindingConfig,
)
from ol_infrastructure.components.aws.acm import ACMCertificate, ACMCertificateConfig
from ol_infrastructure.components.aws.auto_scale_group import (
    BlockDeviceMapping,
    OLAutoScaleGroupConfig,
    OLAutoScaling,
    OLLaunchTemplateConfig,
    OLLoadBalancerConfig,
    OLTargetGroupConfig,
    TagSpecification,
)
from ol_infrastructure.components.services.k8s import (
    OLApplicationK8s,
    OLApplicationK8sConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SDynamicSecretConfig,
    OLVaultK8SSecret,
    OLVaultK8SStaticSecretConfig,
)
from ol_infrastructure.lib.aws.ec2_helper import InstanceTypes, default_egress_args
from ol_infrastructure.lib.aws.eks_helper import (
    default_psg_egress_args,
    get_default_psg_ingress_args,
    setup_k8s_provider,
)
from ol_infrastructure.lib.consul import consul_key_helper, get_consul_provider
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import parse_stack
from ol_infrastructure.lib.vault import setup_vault_provider

stack_info = parse_stack()
notes_config = Config("edxnotes")
if Config("vault").get("address"):
    setup_vault_provider()
consul_provider = get_consul_provider(stack_info)

network_stack = StackReference(f"infrastructure.aws.network.{stack_info.name}")
policy_stack = StackReference("infrastructure.aws.policies")
dns_stack = StackReference("infrastructure.aws.dns")
consul_stack = StackReference(
    f"infrastructure.consul.{stack_info.env_prefix}.{stack_info.name}"
)
vault_stack = StackReference(f"infrastructure.vault.operations.{stack_info.name}")
edxapp_stack = StackReference(
    f"applications.edxapp.{stack_info.env_prefix}.{stack_info.name}"
)

# Conditionally load EKS and OpenSearch stacks if deploying to Kubernetes
deploy_to_k8s = notes_config.get_bool("deploy_to_k8s")
if deploy_to_k8s:
    cluster_name = notes_config.get("cluster") or "applications"
    cluster_stack = StackReference(
        f"infrastructure.aws.eks.{cluster_name}.{stack_info.name}"
    )
    opensearch_stack = StackReference(
        f"infrastructure.aws.opensearch.{stack_info.env_prefix}.{stack_info.name}"
    )

env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
target_vpc_name = notes_config.get("target_vpc")
openedx_release = (
    OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)
    .release_by_env(stack_info.name)
    .value
)
notes_server_tag = f"edx-notes-server-{env_name}"
target_vpc = network_stack.require_output(target_vpc_name)

dns_zone = dns_stack.require_output(notes_config.require("dns_zone"))
dns_zone_id = dns_zone["id"]

secrets = read_yaml_secrets(Path(f"edx_notes/{env_name}.yaml"))

aws_account = get_caller_identity()
vpc_id = target_vpc["id"]
notes_ami = ec2.get_ami(
    filters=[
        ec2.GetAmiFilterArgs(name="tag:OU", values=[f"{stack_info.env_prefix}"]),
        ec2.GetAmiFilterArgs(name="name", values=["edx_notes-*"]),
        ec2.GetAmiFilterArgs(name="virtualization-type", values=["hvm"]),
        ec2.GetAmiFilterArgs(name="root-device-type", values=["ebs"]),
        ec2.GetAmiFilterArgs(name="tag:openedx_release", values=[openedx_release]),
    ],
    most_recent=True,
    owners=[aws_account.account_id],
)

aws_config = AWSBase(
    tags={
        "OU": notes_config.require("business_unit"),
        "Environment": env_name,
        "Application": "open-edx-notes",
        "Owner": "platform-engineering",
        "openedx_release": openedx_release,
    }
)

notes_instance_role = iam.Role(
    f"edx-notes-{env_name}-instance-role",
    assume_role_policy=json.dumps(
        {
            "Version": "2012-10-17",
            "Statement": {
                "Effect": "Allow",
                "Action": "sts:AssumeRole",
                "Principal": {"Service": "ec2.amazonaws.com"},
            },
        }
    ),
    path=f"/ol-applications/open-edx-notes/{stack_info.env_prefix}/{stack_info.env_suffix}/",
    tags=aws_config.tags,
)

notes_vault_policy = vault.Policy(
    f"edx-notes-{env_name}-vault-policy",
    name=f"edx-notes-{stack_info.env_prefix}",
    policy=Path(__file__)
    .parent.joinpath("edx_notes_policy.hcl")
    .read_text()
    .replace("DEPLOYMENT", f"{stack_info.env_prefix}"),
)
aws_vault_backend = f"aws-{stack_info.env_prefix}"
iam.RolePolicyAttachment(
    f"edx-notes-{env_name}-describe-instance-role-policy",
    policy_arn=policy_stack.require_output("iam_policies")["describe_instances"],
    role=notes_instance_role.name,
)
iam.RolePolicyAttachment(
    f"edx-notes-{env_name}-traefik-route53-records-permission",
    policy_arn=policy_stack.require_output("iam_policies")[
        f"route53_{notes_config.require('dns_zone')}_zone_records"
    ],
    role=notes_instance_role.name,
)

notes_instance_profile = iam.InstanceProfile(
    f"edx-notes-{env_name}-instance-profile",
    role=notes_instance_role.name,
    path="/ol-infrastructure/edx-notes-server/profile/",
)
notes_vault_auth_role = vault.aws.AuthBackendRole(
    f"edx-notes-{env_name}-ami-ec2-vault-auth",
    backend=aws_vault_backend,
    role="edx-notes-server",
    inferred_entity_type="ec2_instance",
    inferred_aws_region=aws_config.region,
    bound_iam_instance_profile_arns=[notes_instance_profile.arn],
    bound_ami_ids=[notes_ami.id],
    bound_account_ids=[aws_account.account_id],
    bound_vpc_ids=[vpc_id],
    token_policies=[notes_vault_policy.name],
    opts=ResourceOptions(delete_before_replace=True),
)

vault.generic.Secret(
    f"edx-notes-{env_name}-configuration-secrets",
    path=f"secret-{stack_info.env_prefix}/edx-notes",
    data_json=json.dumps(secrets),
)

consul_datacenter = consul_stack.require_output("datacenter")

k8s_global_labels = K8sGlobalLabels(
    service=Services.edx_notes,
    ou=notes_config.require("business_unit"),
    stack=stack_info,
).model_dump()

# Deploy to Kubernetes using OLApplicationK8s component
if deploy_to_k8s:
    setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

    # Configure namespace
    namespace = notes_config.get("namespace") or f"{stack_info.env_prefix}-openedx"

    # Determine docker image tag - use digest from environment if available (set by CI),
    # otherwise fall back to the openedx release tag
    docker_image_tag = os.environ.get("EDX_NOTES_DOCKER_DIGEST", openedx_release)

    # Get the VPC that the EKS cluster uses (which has k8s subnets configured)
    # The cluster is typically deployed in the applications VPC
    cluster_vpc = network_stack.require_output("applications_vpc")
    cluster_vpc_id = cluster_vpc["id"]
    k8s_pod_subnet_cidrs = cluster_vpc["k8s_pod_subnet_cidrs"]

    # Create security group for edx-notes application pods
    notes_app_security_group = ec2.SecurityGroup(
        f"edx-notes-app-sg-{env_name}",
        name=f"edx-notes-app-sg-{env_name}",
        description="Security group for edx-notes application pods",
        egress=default_psg_egress_args,
        ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
        vpc_id=cluster_vpc_id,
        tags=aws_config.merged_tags({"Name": f"edx-notes-app-{env_name}"}),
    )

    # Get service URLs from stack references (as Output objects)
    opensearch_cluster = opensearch_stack.require_output("cluster")
    opensearch_endpoint = opensearch_cluster["endpoint"]
    edxapp_output = edxapp_stack.require_output("edxapp")
    edxapp_db_address = edxapp_output["mariadb"]

    # Application configuration (non-sensitive, static values only)
    application_config = {
        "ELASTICSEARCH_DSL_PORT": "443",
        "ELASTICSEARCH_DSL_USE_SSL": "true",
        "ELASTICSEARCH_DSL_VERIFY_CERTS": "false",
        "DB_NAME": "edx_notes_api",
        "DB_PORT": "3306",
    }

    # Read Vault policy template and replace DEPLOYMENT placeholder
    vault_policy_template = (
        Path(__file__).parent.joinpath("edx_notes_policy.hcl").read_text()
    )
    vault_policy_text = vault_policy_template.replace(
        "DEPLOYMENT", stack_info.env_prefix
    )

    # Setup Vault Kubernetes auth using OLEKSAuthBinding
    # EDX Notes doesn't need AWS service access (no S3, SES, etc.)
    notes_app = OLEKSAuthBinding(
        OLEKSAuthBindingConfig(
            application_name="edx-notes",
            namespace=namespace,
            stack_info=stack_info,
            aws_config=aws_config,
            iam_policy_document=None,
            vault_policy_text=vault_policy_text,
            cluster_identities=cluster_stack.require_output("cluster_identities"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            irsa_service_account_name="edx-notes",
            vault_sync_service_account_names="edx-notes-vault",
            k8s_labels=K8sGlobalLabels(
                service=Services.edx_notes,
                ou=BusinessUnit(stack_info.env_prefix),
                stack=stack_info,
            ),
        )
    )

    vault_k8s_resources = notes_app.vault_k8s_resources

    # Create VaultStaticSecret for application secrets and environment config
    # Uses Output.all() to handle pulumi.Output objects for hosts
    static_secret_name = "edx-notes-secrets"  # noqa: S105  # pragma: allowlist secret
    notes_static_secret = Output.all(
        db_host=edxapp_db_address,
        opensearch_host=opensearch_endpoint,
    ).apply(
        lambda kwargs: OLVaultK8SSecret(
            f"edx-notes-{env_name}-static-secret",
            OLVaultK8SStaticSecretConfig(
                name="edx-notes-static-secrets",
                namespace=namespace,
                dest_secret_labels=k8s_global_labels,
                dest_secret_name=static_secret_name,
                labels=k8s_global_labels,
                mount=f"secret-{stack_info.env_prefix}",
                mount_type="kv-v2",
                path=f"edx-notes/{env_name}",
                templates={
                    "DJANGO_SECRET_KEY": '{{ get .Secrets "django_secret_key" }}',
                    "OAUTH_CLIENT_ID": '{{ get .Secrets "oauth_client_id" }}',
                    "OAUTH_CLIENT_SECRET": '{{ get .Secrets "oauth_client_secret" }}',
                    "DB_HOST": kwargs["db_host"],
                    "ELASTICSEARCH_DSL_HOST": kwargs["opensearch_host"],
                },
                refresh_after="1h",
                vaultauth=vault_k8s_resources.auth_name,
            ),
            opts=ResourceOptions(
                delete_before_replace=True,
                depends_on=vault_k8s_resources,
            ),
        )
    )

    # Create VaultDynamicSecret for database credentials
    db_creds_secret_name = "edx-notes-db-creds"  # noqa: S105  # pragma: allowlist secret
    db_creds_secret = OLVaultK8SSecret(
        f"edx-notes-{env_name}-db-creds-secret",
        OLVaultK8SDynamicSecretConfig(
            name="edx-notes-db-creds",
            namespace=namespace,
            dest_secret_labels=k8s_global_labels,
            dest_secret_name=db_creds_secret_name,
            labels=k8s_global_labels,
            mount=f"mariadb-{stack_info.env_prefix}",
            path="creds/notes",
            restart_target_kind="Deployment",
            restart_target_name="edx-notes-app",
            templates={
                "DB_USER": "{{ .Secrets.username }}",
                "DB_PASSWORD": "{{ .Secrets.password }}",
            },
            vaultauth=vault_k8s_resources.auth_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=vault_k8s_resources,
        ),
    )

    # Pre-deploy commands for migrations and Elasticsearch index
    pre_deploy_commands = [
        ("migrate", ["python", "manage.py", "migrate", "--noinput"]),
        ("es-index", ["python", "manage.py", "search_index", "--rebuild", "-f"]),
    ]

    # Build OLApplicationK8s config
    ol_app_k8s_config = OLApplicationK8sConfig(
        project_root=Path(__file__).parent,
        application_config=application_config,
        application_name="edx-notes",
        application_namespace=namespace,
        application_lb_service_name="edx-notes",
        application_lb_service_port_name="http",
        application_min_replicas=notes_config.get_int("min_replicas") or 1,
        application_max_replicas=notes_config.get_int("max_replicas") or 3,
        application_deployment_use_anti_affinity=True,
        k8s_global_labels=k8s_global_labels,
        env_from_secret_names=["edx-notes-secrets", db_creds_secret_name],
        application_security_group_id=notes_app_security_group.id,
        application_security_group_name=notes_app_security_group.name,
        application_service_account_name=None,
        application_image_repository="mitodl/openedx-notes",
        application_docker_tag=docker_image_tag,
        application_cmd_array=None,
        application_arg_array=None,
        vault_k8s_resource_auth_name=f"edx-notes-{stack_info.env_prefix}",
        registry="dockerhub",
        image_pull_policy="IfNotPresent",
        import_nginx_config=False,  # EDX Notes doesn't use nginx/uwsgi pattern
        init_migrations=False,  # We're using pre-deploy jobs instead
        init_collectstatic=False,  # EDX Notes doesn't need collectstatic
        resource_requests={
            "cpu": notes_config.get("cpu_request") or "250m",
            "memory": notes_config.get("memory_request") or "512Mi",
        },
        resource_limits={
            "cpu": notes_config.get("cpu_limit") or "500m",
            "memory": notes_config.get("memory_limit") or "1Gi",
        },
        pre_deploy_commands=pre_deploy_commands,
        probe_configs={
            "liveness_probe": kubernetes.core.v1.ProbeArgs(
                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                    path="/heartbeat",
                    port=8000,
                ),
                initial_delay_seconds=30,
                period_seconds=10,
            ),
            "readiness_probe": kubernetes.core.v1.ProbeArgs(
                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                    path="/heartbeat",
                    port=8000,
                ),
                initial_delay_seconds=10,
                period_seconds=5,
            ),
            "startup_probe": kubernetes.core.v1.ProbeArgs(
                http_get=kubernetes.core.v1.HTTPGetActionArgs(
                    path="/heartbeat",
                    port=8000,
                ),
                initial_delay_seconds=10,
                period_seconds=10,
                failure_threshold=6,
            ),
        },
    )

    # Create the OLApplicationK8s component
    edx_notes_k8s_app = OLApplicationK8s(
        ol_app_k8s_config=ol_app_k8s_config,
    )

    # HTTPRoute for Gateway API (if needed - depends on your ingress strategy)
    dns_name = notes_config.get("domain")
    notes_httproute = kubernetes.apiextensions.CustomResource(
        f"edx-notes-httproute-{env_name}",
        api_version="gateway.networking.k8s.io/v1",
        kind="HTTPRoute",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name="edx-notes",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "parentRefs": [
                {
                    "name": "default-gateway",
                    "namespace": "gateway-system",
                }
            ],
            "hostnames": [dns_name],
            "rules": [
                {
                    "matches": [
                        {
                            "path": {
                                "type": "PathPrefix",
                                "value": "/",
                            }
                        }
                    ],
                    "backendRefs": [
                        {
                            "name": "edx-notes",
                            "port": 8000,
                        }
                    ],
                }
            ],
        },
    )

    # Export Kubernetes resources
    export("k8s_deployment_name", "edx-notes-app")
    export("k8s_service_name", "edx-notes")
    export("k8s_namespace", namespace)
    export("notes_domain", dns_name)
    export("security_group_id", notes_app_security_group.id)

# Deploy to EC2 (original implementation)
if not deploy_to_k8s:
    notes_security_group = ec2.SecurityGroup(
        f"edx-notes-{env_name}-security-group",
        name=f"edx-notes-{target_vpc_name}-{env_name}",
        description="Access control for notes severs.",
        ingress=[
            ec2.SecurityGroupIngressArgs(
                protocol="tcp",
                from_port=DEFAULT_HTTPS_PORT,
                to_port=DEFAULT_HTTPS_PORT,
                cidr_blocks=["0.0.0.0/0"],
                description=(
                    f"Allow traffic to the notes server on port {DEFAULT_HTTPS_PORT}"
                ),
            ),
        ],
        egress=default_egress_args,
        vpc_id=vpc_id,
        tags=aws_config.merged_tags({"Name": notes_server_tag}),
    )

    cert_config = ACMCertificateConfig(
        certificate_domain=notes_config.require("acm_cert_domain"),
        certificate_zone_id=dns_zone_id,
        certificate_tags=aws_config.tags,
    )
    notes_certificate = ACMCertificate(name="edx-notes", cert_config=cert_config)

    lb_config = OLLoadBalancerConfig(
        listener_use_acm=True,
        listener_cert_arn=notes_certificate.validated_certificate.certificate_arn,
        subnets=target_vpc["subnet_ids"],
        security_groups=[notes_security_group],
        tags=aws_config.merged_tags({"Name": notes_server_tag}),
    )

    tg_config = OLTargetGroupConfig(
        vpc_id=vpc_id,
        health_check_interval=60,
        health_check_matcher="404",
        health_check_path="/",
        tags=aws_config.merged_tags({"Name": notes_server_tag}),
    )

    block_device_mappings = [BlockDeviceMapping()]
    tag_specs = [
        TagSpecification(
            resource_type="instance",
            tags=aws_config.merged_tags({"Name": notes_server_tag}),
        ),
        TagSpecification(
            resource_type="volume",
            tags=aws_config.merged_tags({"Name": notes_server_tag}),
        ),
    ]

    grafana_credentials = read_yaml_secrets(
        Path(f"vector/grafana.{stack_info.env_suffix}.yaml")
    )

    lt_config = OLLaunchTemplateConfig(
        block_device_mappings=block_device_mappings,
        image_id=notes_ami.id,
        instance_type=notes_config.get("instance_type")
        or InstanceTypes.burstable_micro,
        instance_profile_arn=notes_instance_profile.arn,
        security_groups=[
            notes_security_group,
            consul_stack.require_output("security_groups")["consul_agent"],
        ],
        tag_specifications=tag_specs,
        tags=aws_config.merged_tags({"Name": notes_server_tag}),
        user_data=consul_datacenter.apply(
            lambda consul_dc: base64.b64encode(
                "#cloud-config\n{}".format(
                    yaml.dump(
                        {
                            "write_files": [
                                {
                                    "path": "/etc/consul.d/02-autojoin.json",
                                    "content": json.dumps(
                                        {
                                            "retry_join": [
                                                "provider=aws tag_key=consul_env "
                                                f"tag_value={consul_dc}"
                                            ],
                                            "datacenter": consul_dc,
                                        }
                                    ),
                                    "owner": "consul:consul",
                                },
                                {
                                    "path": "/etc/default/vector",
                                    "content": textwrap.dedent(
                                        f"""\
                                ENVIRONMENT={consul_dc}
                                APPLICATION=notes
                                SERVICE=openedx
                                VECTOR_CONFIG_DIR=/etc/vector/
                                VECTOR_STRICT_ENV_VARS=false
                                AWS_REGION={aws_config.region}
                                GRAFANA_CLOUD_API_KEY={grafana_credentials["api_key"]}
                                GRAFANA_CLOUD_PROMETHEUS_API_USER={grafana_credentials["prometheus_user_id"]}
                                GRAFANA_CLOUD_LOKI_API_USER={grafana_credentials["loki_user_id"]}
                                """
                                    ),
                                    "owner": "root:root",
                                },
                            ],
                        },
                        sort_keys=True,
                    )
                ).encode("utf8")
            ).decode("utf8")
        ),
    )

    auto_scale_config = notes_config.get_object("auto_scale") or {
        "desired": 1,
        "min": 1,
        "max": 2,
    }
    asg_config = OLAutoScaleGroupConfig(
        asg_name=f"edx-notes-{env_name}",
        aws_config=aws_config,
        desired_size=auto_scale_config["desired"],
        min_size=auto_scale_config["min"],
        max_size=auto_scale_config["max"],
        vpc_zone_identifiers=target_vpc["subnet_ids"],
        tags=aws_config.merged_tags({"Name": notes_server_tag}),
    )
    as_setup = OLAutoScaling(
        asg_config=asg_config,
        lt_config=lt_config,
        tg_config=tg_config,
        lb_config=lb_config,
    )

    dns_name = notes_config.get("domain")

    consul_keys = {
        "edx/release": openedx_release,
        "edx/notes-api-host": dns_name,
        "edx/deployment": f"{stack_info.env_prefix}",
    }
    consul.Keys(
        f"edx-notes-{env_name}-configuration-data",
        keys=consul_key_helper(consul_keys),
        opts=consul_provider,
    )

    five_minutes = 60 * 5

    route53.Record(
        f"edx-notes-{env_name}-dns-record",
        name=dns_name,
        type="CNAME",
        ttl=five_minutes,
        records=[as_setup.load_balancer.dns_name],
        zone_id=dns_zone_id,
    )

    export("security_group_id", notes_security_group.id)
    export("notes_domain", dns_name)
