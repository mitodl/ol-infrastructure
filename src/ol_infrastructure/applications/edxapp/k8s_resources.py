# ruff: noqa: F841, E501, PLR0913, FIX002, PLR0915
import os

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import iam

from bridge.settings.openedx.types import OpenEdxSupportedRelease
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.applications.edxapp.k8s_configmaps import create_k8s_configmaps
from ol_infrastructure.applications.edxapp.k8s_ingress_resources import (
    create_k8s_ingress_resources,
)
from ol_infrastructure.applications.edxapp.k8s_secrets import create_k8s_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.aws.database import OLAmazonDB
from ol_infrastructure.components.aws.eks import (
    OLEKSTrustRole,
    OLEKSTrustRoleConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    default_psg_egress_args,
    get_default_psg_ingress_args,
)
from ol_infrastructure.lib.ol_types import (
    AWSBase,
    BusinessUnit,
    K8sGlobalLabels,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import StackInfo


def create_k8s_resources(
    aws_config: AWSBase,
    cluster_stack: StackReference,
    edxapp_cache: OLAmazonCache,
    edxapp_config: Config,
    edxapp_db: OLAmazonDB,
    edxapp_iam_policy: aws.iam.Policy,
    mongodb_atlas_stack: StackReference,
    network_stack: StackReference,
    notes_stack: StackReference,
    stack_info: StackInfo,
    vault_config: Config,
    vault_policy: vault.Policy,
):
    env_name = f"{stack_info.env_prefix}-{stack_info.env_suffix}"
    lms_webapp_deployment_name = f"{env_name}-edxapp-lms-webapp"
    cms_webapp_deployment_name = f"{env_name}-edxapp-cms-webapp"

    lms_celery_deployment_name = f"{env_name}-edxapp-lms-celery"
    cms_celery_deployment_name = f"{env_name}-edxapp-cms-celery"

    aws_account = aws.get_caller_identity()

    # Get various VPC / network configuration information
    apps_vpc = network_stack.require_output("applications_vpc")
    k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

    # Verify that the namespace exists in the EKS cluster
    data_vpc = network_stack.require_output("data_vpc")
    operations_vpc = network_stack.require_output("operations_vpc")
    edxapp_target_vpc = (
        edxapp_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
    )
    edxapp_vpc = network_stack.require_output(edxapp_target_vpc)

    # TODO(Mike): Will require special handling for residential clusters
    k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]

    # Verify that the namespace exists in the EKS cluster
    namespace = f"{stack_info.env_prefix}-openedx"
    cluster_stack.require_output("namespaces").apply(
        lambda ns: check_cluster_namespace(namespace, ns)
    )

    # Look up what what release to deploy for this stack
    release_info = OpenLearningOpenEdxDeployment.get_item(stack_info.env_prefix)

    opensearch_stack = StackReference(
        f"infrastructure.aws.opensearch.{stack_info.env_prefix}.{stack_info.name}"
    )
    opensearch_hostname = opensearch_stack.require_output("cluster")["endpoint"]

    # Configure reusable global labels
    k8s_global_labels = K8sGlobalLabels(
        service=Services.edxapp,
        ou=BusinessUnit(stack_info.env_prefix),
        stack=stack_info,
    ).model_dump()

    # We need to "guess" service account name here because there is a loop with
    # needing the service account name for the trust role, but needing the trustrole
    # for the annotations on the service account created by OLVaultK8SResources.
    edxapp_service_account_name = f"{stack_info.env_prefix}-edxapp-vault"

    # The OLEKSTrustRole component appears to have an issue handling Pulumi Outputs as
    # inputs. This can be worked around by resolving the outputs within an `apply` and
    # then instantiating the component.
    edxapp_trust_role = OLEKSTrustRole(
        f"ol-{stack_info.env_prefix}-edxapp-trustrole-{stack_info.env_suffix}",
        role_config=OLEKSTrustRoleConfig(
            account_id=aws_account.account_id,
            cluster_name=f"applications-{stack_info.name}",
            cluster_identities=cluster_stack.require_output("cluster_identities"),
            description=f"Trust role for allowing the {env_name} edxapp "
            "application to access AWS resources",
            policy_operator="StringEquals",
            role_name=f"edxapp-trustrole-{env_name}",
            service_account_identifier=(
                f"system:serviceaccount:{namespace}:{edxapp_service_account_name}"
            ),
            tags=aws_config.tags,
        ),
    )

    iam.RolePolicyAttachment(
        f"ol-{stack_info.env_prefix}-edxapp-trustrole-policy-attachment-{stack_info.env_suffix}",
        policy_arn=edxapp_iam_policy.arn,
        role=edxapp_trust_role.role.name,
    )

    # Create a kubernetes vault auth backend role
    edxapp_k8s_vault_auth_backend_role = vault.kubernetes.AuthBackendRole(
        f"ol-{stack_info.env_prefix}-vault-k8s-auth-backend-role-{stack_info.env_suffix}",
        role_name=f"{stack_info.env_prefix}-edxapp",
        backend=cluster_stack.require_output("vault_auth_endpoint"),
        bound_service_account_names=["*"],
        bound_service_account_namespaces=[namespace],
        token_policies=[vault_policy.name],
    )

    # Setup all the bits and bobs needed to interact with vault from k8s
    vault_k8s_resources = OLVaultK8SResources(
        resource_config=OLVaultK8SResourcesConfig(
            application_name=f"{stack_info.env_prefix}-edxapp",
            namespace=namespace,
            labels=k8s_global_labels,
            annotations={"eks.amazonaws.com/role-arn": edxapp_trust_role.role.arn},
            vault_address=vault_config.require("address"),
            vault_auth_endpoint=cluster_stack.require_output("vault_auth_endpoint"),
            vault_auth_role_name=edxapp_k8s_vault_auth_backend_role.role_name,
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[edxapp_k8s_vault_auth_backend_role],
        ),
    )

    # The edxapp application in Kubernetes requires its own security group
    edxapp_k8s_app_security_group = aws.ec2.SecurityGroup(
        f"ol-{stack_info.env_prefix}-edxapp-k8s-app-security-group-{stack_info.env_suffix}",
        name_prefix=f"{env_name}",
        description="Security group for the edxapp application in Kubernetes",
        egress=default_psg_egress_args,
        ingress=get_default_psg_ingress_args(k8s_pod_subnet_cidrs=k8s_pod_subnet_cidrs),
        tags=aws_config.tags,
        vpc_id=apps_vpc["id"],
    )
    export("edxapp_k8s_app_security_group_id", edxapp_k8s_app_security_group.id)

    # Tell EKS to hook CMS and LMS pods to the application security group
    kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-sg-policy-{stack_info.env_suffix}",
        api_version="vpcresources.k8s.aws/v1beta1",
        kind="SecurityGroupPolicy",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-edxapp-sg-policy",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec={
            "podSelector": {
                "matchLabels": {
                    "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
                }
            },
            "securityGroups": {"groupIds": [edxapp_k8s_app_security_group.id]},
        },
        opts=pulumi.ResourceOptions(depends_on=[edxapp_k8s_app_security_group]),
    )

    secrets = create_k8s_secrets(
        edxapp_cache=edxapp_cache,
        edxapp_config=edxapp_config,
        edxapp_db=edxapp_db,
        k8s_global_labels=k8s_global_labels,
        mongodb_atlas_stack=mongodb_atlas_stack,
        namespace=namespace,
        stack_info=stack_info,
        vault_k8s_resources=vault_k8s_resources,
    )

    configmaps = create_k8s_configmaps(
        stack_info=stack_info,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        edxapp_config=edxapp_config,
        edxapp_cache=edxapp_cache,
        notes_stack=notes_stack,
        opensearch_hostname=opensearch_hostname,
    )

    # Lookup environment vars needed for the deployment
    if "EDXAPP_DOCKER_IMAGE_DIGEST" not in os.environ:
        msg = "Environment variable EDXAPP_DOCKER_IMAGE_DIGEST is not set. "
        raise OSError(msg)
    EDXAPP_DOCKER_IMAGE_DIGEST = os.environ["EDXAPP_DOCKER_IMAGE_DIGEST"]

    OPENEDX_RELEASE: OpenEdxSupportedRelease = os.environ.get(
        "OPENEDX_RELEASE", "master"
    )

    ############################################
    # Setup an init container that downloads and extracts the staticfiles
    ############################################
    edxapp_image = f"610119931565.dkr.ecr.us-east-1.amazonaws.com/dockerhub/mitodl/edxapp@{EDXAPP_DOCKER_IMAGE_DIGEST}"
    production_staticfiles_archive_name = (
        f"staticfiles-production-{EDXAPP_DOCKER_IMAGE_DIGEST}.tar.gz"
    )
    nonprod_staticfiles_archive_name = (
        f"staticfiles-nonprod-{EDXAPP_DOCKER_IMAGE_DIGEST}.tar.gz"
    )
    production_staticfiles_url = f"https://ol-eng-artifacts.s3.amazonaws.com/edx-staticfiles/{stack_info.env_prefix}/{OPENEDX_RELEASE}/{production_staticfiles_archive_name}"
    nonprod_staticfiles_url = f"https://ol-eng-artifacts.s3.amazonaws.com/edx-staticfiles/{stack_info.env_prefix}/{OPENEDX_RELEASE}/{nonprod_staticfiles_archive_name}"

    prod_dl_command = f"wget {production_staticfiles_url} -O /tmp/prod.tar.gz && tar -xf /tmp/prod.tar.gz --strip-components 2 -C /openedx/staticfiles"
    nonprod_dl_command = f"wget {nonprod_staticfiles_url} -O /tmp/nonprod.tar.gz && tar -xf /tmp/nonprod.tar.gz --strip-components 2 -C /openedx/staticfiles"

    if stack_info.env_suffix == "production":
        dl_command = prod_dl_command
    else:
        dl_command = nonprod_dl_command
    staticfiles_init_container_command = [
        "/bin/sh",
        "-c",
        f"({dl_command}) || echo 'Could not download staticfiles'",
    ]

    staticfiles_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name="staticfiles",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        ),
    ]

    staticfiles_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name="staticfiles",
            mount_path="/openedx/staticfiles",
        ),
    ]

    staticfiles_init_container = kubernetes.core.v1.ContainerArgs(
        name="staticfiles-downloader",
        image="busybox:1.35",
        command=staticfiles_init_container_command,
        volume_mounts=staticfiles_volume_mounts,
    )

    common_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name="edxapp-config",
            mount_path="/openedx/config",
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="openedx-data",
            mount_path="/openedx/data",
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="openedx-logs",
            mount_path="/openedx/data/logs",
        ),
        kubernetes.core.v1.VolumeMountArgs(
            name="openedx-media",
            mount_path="/openedx/media/",
        ),
        *staticfiles_volume_mounts,
    ]

    webapp_volume_mounts = [
        *common_volume_mounts,
        kubernetes.core.v1.VolumeMountArgs(
            name=configmaps.uwsgi_ini_config_name,
            mount_path="/openedx/edx-platform/uwsgi.ini",
            sub_path="uwsgi.ini",
        ),
    ]

    ############################################
    # cms deployment resources
    ############################################
    # All of the secrets and configmaps that will be mounted into the edxapp cms containers
    # The names are prefixed with numbers to control the order they are concatenated in.
    cms_edxapp_config_sources = {
        secrets.db_creds_secret_name: secrets.db_creds,
        secrets.db_connections_secret_name: secrets.db_connections,
        secrets.mongo_db_creds_secret_name: secrets.mongo_db_creds,
        secrets.mongo_db_forum_secret_name: secrets.mongo_db_forum,
        secrets.general_secrets_name: secrets.general,
        secrets.xqueue_secret_name: secrets.xqueue,
        secrets.forum_secret_name: secrets.forum,
        configmaps.general_config_name: configmaps.general,
        configmaps.interpolated_config_name: configmaps.interpolated,
        # Just CMS specific resources below this line
        secrets.cms_oauth_secret_name: secrets.cms_oauth,
        configmaps.cms_general_config_name: configmaps.cms_general,
        configmaps.cms_interpolated_config_name: configmaps.cms_interpolated,
    }
    cms_edxapp_secret_names = [
        secrets.db_creds_secret_name,
        secrets.db_connections_secret_name,
        secrets.mongo_db_creds_secret_name,
        secrets.mongo_db_forum_secret_name,
        secrets.general_secrets_name,
        secrets.xqueue_secret_name,
        secrets.forum_secret_name,
        # Just CMS specific resources below this line
        secrets.cms_oauth_secret_name,
    ]
    cms_edxapp_configmap_names = [
        configmaps.general_config_name,
        configmaps.interpolated_config_name,
        # Just CMS specific resources below this line
        configmaps.cms_general_config_name,
        configmaps.cms_interpolated_config_name,
    ]

    # Define the volumes that will be mounted into the edxapp containers
    cms_edxapp_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name=secret_name,
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(secret_name=secret_name),
        )
        for secret_name in cms_edxapp_secret_names
    ]
    cms_edxapp_volumes.extend(
        [
            kubernetes.core.v1.VolumeArgs(
                name=configmap_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmap_name
                ),
            )
            for configmap_name in cms_edxapp_configmap_names
        ]
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="edxapp-config",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-data",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-logs",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-media",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name=configmaps.uwsgi_ini_config_name,
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=configmaps.uwsgi_ini_config_name
            ),
        )
    )
    cms_edxapp_volumes.extend(staticfiles_volumes)

    # Define the volume mounts for the init container that aggregates the config files
    cms_edxapp_init_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name=source_name,
            mount_path=f"/openedx/config-sources/{source_name}",
            read_only=True,
        )
        for source_name in cms_edxapp_config_sources
    ]
    cms_edxapp_init_volume_mounts.append(
        kubernetes.core.v1.VolumeMountArgs(
            name="edxapp-config",
            mount_path="/openedx/config",
        )
    )
    cms_webapp_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-cms-webapp",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    cms_webapp_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-cms-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=cms_webapp_deployment_name,
            namespace=namespace,
            labels=cms_webapp_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=cms_webapp_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=cms_webapp_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=cms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image="busybox:1.35",
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/cms.env.yml"
                            ],
                            volume_mounts=cms_edxapp_init_volume_mounts,
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="cms-edxapp",
                            image=edxapp_image,
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="cms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="cms.envs.production",
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="UWSGI_WORKERS", value="2"
                                ),
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    container_port=8000, name="http"
                                )
                            ],
                            volume_mounts=webapp_volume_mounts,
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=list(cms_edxapp_config_sources.values())
        ),
    )
    cms_webapp_service = kubernetes.core.v1.Service(
        f"ol-{stack_info.env_prefix}-edxapp-cms-service-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=cms_webapp_deployment_name,
            namespace=namespace,
            labels=cms_webapp_labels,
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            ports=[
                kubernetes.core.v1.ServicePortArgs(
                    name="http",
                    port=8000,
                    # 'http' is the name of the port in the deployment podspec
                    target_port="http",
                    protocol="TCP",
                )
            ],
            selector=cms_webapp_labels,
            type="ClusterIP",
        ),
    )

    cms_celery_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-cms-celery",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    cms_celery_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-cms-celery-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{cms_celery_deployment_name}",
            namespace=namespace,
            labels=cms_celery_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=cms_celery_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=cms_celery_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=cms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,  # strictly speaking, not required
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image="busybox:1.35",
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/cms.env.yml"
                            ],
                            volume_mounts=cms_edxapp_init_volume_mounts,
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="cms-edxapp",
                            image=edxapp_image,
                            command=["celery"],
                            args=[
                                "--app=cms.celery",
                                "worker",
                                "-B",
                                "-E",
                                "--loglevel=info",
                                "--hostname=edx.cms.core.default.%%h",
                                "--max-tasks-per-child",
                                "100",
                                "--exclude-queues=edx.lms.core.default",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="cms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="cms.envs.production",
                                ),
                            ],
                            volume_mounts=common_volume_mounts,
                        )
                    ],
                ),
            ),
        ),
    )

    ############################################
    # lms deployment resources
    ############################################
    lms_edxapp_config_sources = {
        secrets.db_creds_secret_name: secrets.db_creds,
        secrets.db_connections_secret_name: secrets.db_connections,
        secrets.mongo_db_creds_secret_name: secrets.mongo_db_creds,
        secrets.mongo_db_forum_secret_name: secrets.mongo_db_forum,
        secrets.general_secrets_name: secrets.general,
        secrets.xqueue_secret_name: secrets.xqueue,
        secrets.forum_secret_name: secrets.forum,
        configmaps.general_config_name: configmaps.general,
        configmaps.interpolated_config_name: configmaps.interpolated,
        # Just LMS specific resources below this line
        secrets.lms_oauth_secret_name: secrets.lms_oauth,
        configmaps.lms_general_config_name: configmaps.lms_general,
        configmaps.lms_interpolated_config_name: configmaps.lms_interpolated,
    }
    lms_edxapp_secret_names = [
        secrets.db_creds_secret_name,
        secrets.db_connections_secret_name,
        secrets.mongo_db_creds_secret_name,
        secrets.mongo_db_forum_secret_name,
        secrets.general_secrets_name,
        secrets.xqueue_secret_name,
        secrets.forum_secret_name,
        # Just LMS specific resources below this line
        secrets.lms_oauth_secret_name,
    ]
    lms_edxapp_configmap_names = [
        configmaps.general_config_name,
        configmaps.interpolated_config_name,
        # Just LMS specific resources below this line
        configmaps.lms_general_config_name,
        configmaps.lms_interpolated_config_name,
    ]

    # Define the volumes that will be mounted into the edxapp containers
    lms_edxapp_volumes = [
        kubernetes.core.v1.VolumeArgs(
            name=secret_name,
            secret=kubernetes.core.v1.SecretVolumeSourceArgs(secret_name=secret_name),
        )
        for secret_name in lms_edxapp_secret_names
    ]
    lms_edxapp_volumes.extend(
        [
            kubernetes.core.v1.VolumeArgs(
                name=configmap_name,
                config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                    name=configmap_name
                ),
            )
            for configmap_name in lms_edxapp_configmap_names
        ]
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="edxapp-config",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-data",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-logs",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-media",
            empty_dir=kubernetes.core.v1.EmptyDirVolumeSourceArgs(),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name=configmaps.uwsgi_ini_config_name,
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=configmaps.uwsgi_ini_config_name
            ),
        )
    )
    lms_edxapp_volumes.extend(staticfiles_volumes)

    # Define the volume mounts for the init container that aggregates the config files
    lms_edxapp_init_volume_mounts = [
        kubernetes.core.v1.VolumeMountArgs(
            name=source_name,
            mount_path=f"/openedx/config-sources/{source_name}",
            read_only=True,
        )
        for source_name in lms_edxapp_config_sources
    ]
    lms_edxapp_init_volume_mounts.append(
        kubernetes.core.v1.VolumeMountArgs(
            name="edxapp-config",
            mount_path="/openedx/config",
        )
    )

    lms_webapp_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-lms-webapp",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    lms_webapp_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-lms-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=lms_webapp_deployment_name,
            namespace=namespace,
            labels=lms_webapp_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=lms_webapp_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=lms_webapp_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image="busybox:1.35",
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml"
                            ],
                            volume_mounts=lms_edxapp_init_volume_mounts,
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="lms-edxapp",
                            image=edxapp_image,
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="lms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="lms.envs.production",
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="UWSGI_WORKERS", value="2"
                                ),
                            ],
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    container_port=8000, name="http"
                                )
                            ],
                            volume_mounts=webapp_volume_mounts,
                        ),
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=list(lms_edxapp_config_sources.values())
        ),
    )
    lms_webapp_service = kubernetes.core.v1.Service(
        f"ol-{stack_info.env_prefix}-edxapp-lms-service-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=lms_webapp_deployment_name,
            namespace=namespace,
            labels=lms_webapp_labels,
        ),
        spec=kubernetes.core.v1.ServiceSpecArgs(
            ports=[
                kubernetes.core.v1.ServicePortArgs(
                    name="http",
                    port=8000,
                    # 'http' is the name of the port in the deployment podspec
                    target_port="http",
                    protocol="TCP",
                )
            ],
            selector=lms_webapp_labels,
            type="ClusterIP",
        ),
    )
    lms_celery_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-lms-celery",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    lms_celery_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-lms-celery-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{lms_celery_deployment_name}",
            namespace=namespace,
            labels=lms_celery_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=lms_celery_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=lms_celery_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,  # strictly speaking, not required
                        kubernetes.core.v1.ContainerArgs(
                            name="config-aggregator",
                            image="busybox:1.35",
                            command=["/bin/sh", "-c"],
                            args=[
                                "cat /openedx/config-sources/*/*.yaml > /openedx/config/lms.env.yml"
                            ],
                            volume_mounts=lms_edxapp_init_volume_mounts,
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="lms-edxapp",
                            image=edxapp_image,
                            command=["celery"],
                            args=[
                                "--app=lms.celery",
                                "worker",
                                "-B",
                                "-E",
                                "--loglevel=info",
                                "--hostname=edx.lms.core.default.%%h",
                                "--max-tasks-per-child",
                                "100",
                                "--exclude-queues=edx.lms.core.default",
                            ],
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="SERVICE_VARIANT", value="lms"
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="DJANGO_SETTINGS_MODULE",
                                    value="lms.envs.production",
                                ),
                            ],
                            volume_mounts=common_volume_mounts,
                        )
                    ],
                ),
            ),
        ),
    )

    create_k8s_ingress_resources(
        edxapp_config=edxapp_config,
        stack_info=stack_info,
        namespace=namespace,
        k8s_global_labels=k8s_global_labels,
        lms_webapp_deployment_name=lms_webapp_deployment_name,
        cms_webapp_deployment_name=cms_webapp_deployment_name,
        lms_webapp_deployment=lms_webapp_deployment,
        cms_webapp_deployment=cms_webapp_deployment,
    )

    return {
        "edxapp_k8s_app_security_group_id": edxapp_k8s_app_security_group.id,
    }
