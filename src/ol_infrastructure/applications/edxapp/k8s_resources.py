# ruff: noqa: F841, E501, PLR0913, PLR0915
import os
from pathlib import Path

import pulumi
import pulumi_aws as aws
import pulumi_kubernetes as kubernetes
import pulumi_vault as vault
from pulumi import Config, ResourceOptions, StackReference, export
from pulumi_aws import iam

from bridge.lib.magic_numbers import DEFAULT_REDIS_PORT
from bridge.settings.openedx.types import OpenEdxSupportedRelease
from bridge.settings.openedx.version_matrix import OpenLearningOpenEdxDeployment
from ol_infrastructure.applications.edxapp.k8s_configmaps import create_k8s_configmaps
from ol_infrastructure.applications.edxapp.k8s_secrets import create_k8s_secrets
from ol_infrastructure.components.aws.cache import OLAmazonCache
from ol_infrastructure.components.aws.database import OLAmazonDB
from ol_infrastructure.components.aws.eks import (
    OLEKSTrustRole,
    OLEKSTrustRoleConfig,
)
from ol_infrastructure.components.services.cert_manager import (
    OLCertManagerCert,
    OLCertManagerCertConfig,
)
from ol_infrastructure.components.services.k8s import (
    OLApisixRoute,
    OLApisixRouteConfig,
    OLApisixSharedPlugins,
    OLApisixSharedPluginsConfig,
)
from ol_infrastructure.components.services.vault import (
    OLVaultK8SResources,
    OLVaultK8SResourcesConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    cached_image_uri,
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


def create_k8s_resources(  # noqa: C901
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

    replicas_dict = edxapp_config.require_object("k8s_replicas")
    resources_dict = edxapp_config.require_object("k8s_resources")

    def _get_resources_requests_limits(
        class_name: str, component: str
    ) -> kubernetes.core.v1.ResourceRequirementsArgs:
        return kubernetes.core.v1.ResourceRequirementsArgs(
            requests={
                "cpu": resources_dict[class_name][component]["cpu_request"],
                "memory": resources_dict[class_name][component]["memory_request"],
            },
            limits={
                "memory": resources_dict[class_name][component]["memory_limit"],
            },
        )

    # Get various VPC / network configuration information
    data_vpc = network_stack.require_output("data_vpc")
    operations_vpc = network_stack.require_output("operations_vpc")
    edxapp_target_vpc = (
        edxapp_config.get("target_vpc") or f"{stack_info.env_prefix}_vpc"
    )
    edxapp_vpc = network_stack.require_output(edxapp_target_vpc)

    # For K8s deployments, the cluster and pod subnets are in the cluster VPC
    # This is typically the applications_vpc for standard deployments, but may be
    # a residential VPC or other target VPC for specialized deployments.
    # We need to get the K8s pod subnets from the cluster's VPC, not from a hardcoded applications_vpc.
    # The cluster is deployed in the same VPC where the pods will run.
    # When using residential clusters, this will be the residential_mitx_vpc, etc.
    # For now, we use the edxapp_vpc which is the target VPC. For k8s deployments,
    # this should be the cluster VPC. We get the applications_vpc as fallback for compatibility.
    # Try to get pod subnets from the target VPC first (handles residential, xpro, etc.)
    cluster_vpc = edxapp_vpc
    if "k8s_pod_subnet_cidrs" in cluster_vpc:
        k8s_pod_subnet_cidrs = cluster_vpc["k8s_pod_subnet_cidrs"]
    else:
        # Fallback to applications_vpc for standard deployments
        apps_vpc = network_stack.require_output("applications_vpc")
        k8s_pod_subnet_cidrs = apps_vpc["k8s_pod_subnet_cidrs"]
        cluster_vpc = apps_vpc

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
    if stack_info.env_prefix == "xpro":
        ou = BusinessUnit.xpro
    else:
        ou = BusinessUnit(stack_info.env_prefix)
    k8s_global_labels = K8sGlobalLabels(
        service=Services.edxapp,
        ou=ou,
        stack=stack_info,
    ).model_dump()

    # Create ConfigMap for Vector configuration
    vector_config_path = Path(__file__).parent / "files/vector/edxapp_tracking_log.yaml"
    with vector_config_path.open() as f:
        vector_config_content = f.read()

    vector_configmap = kubernetes.core.v1.ConfigMap(
        f"ol-{stack_info.env_prefix}-edxapp-vector-config-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-edxapp-vector-config",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        data={
            "vector.yaml": vector_config_content,
        },
    )

    # We need to "guess" service account name here because there is a loop with
    # needing the service account name for the trust role, but needing the trustrole
    # for the annotations on the service account created by OLVaultK8SResources.
    edxapp_service_account_name = f"{stack_info.env_prefix}-edxapp-vault"

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
        vpc_id=cluster_vpc["id"],
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

    webapp_hpa_scaling_metrics = [
        kubernetes.autoscaling.v2.MetricSpecArgs(
            type="Resource",
            resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                name="cpu",
                target=kubernetes.autoscaling.v2.MetricTargetArgs(
                    type="Utilization",
                    average_utilization=80,
                ),
            ),
        ),
        kubernetes.autoscaling.v2.MetricSpecArgs(
            type="Resource",
            resource=kubernetes.autoscaling.v2.ResourceMetricSourceArgs(
                name="memory",
                target=kubernetes.autoscaling.v2.MetricTargetArgs(
                    type="Utilization",
                    average_utilization=80,
                ),
            ),
        ),
    ]
    webapp_hpa_behavior = kubernetes.autoscaling.v2.HorizontalPodAutoscalerBehaviorArgs(
        scale_up=kubernetes.autoscaling.v2.HPAScalingRulesArgs(
            stabilization_window_seconds=60,  # wait 1 minute before scaling uzp again
            select_policy="Max",  # Choose the max value when multiple metrics
            policies=[
                kubernetes.autoscaling.v2.HPAScalingPolicyArgs(
                    type="Percent",
                    value=100,  # at most, double the pods
                    period_seconds=60,  # within a minute
                )
            ],
        ),
        scale_down=kubernetes.autoscaling.v2.HPAScalingRulesArgs(
            stabilization_window_seconds=300,  # wait 5 minutes before scaling down again
            select_policy="Min",  # Choose the max value when multiple metrics
            policies=[
                kubernetes.autoscaling.v2.HPAScalingPolicyArgs(
                    type="Percent",
                    value=25,  # at most, remove 1/4 of the pods at once
                    period_seconds=60,  # within 1 minute
                )
            ],
        ),
    )

    # Call out to other modules to create the k8s secrets and configmaps
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

    openedx_data_pvc = kubernetes.core.v1.PersistentVolumeClaim(
        f"ol-{stack_info.env_prefix}-openedx-data-pvc-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-openedx-data-pvc",
            namespace=namespace,
            labels=k8s_global_labels,
        ),
        spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
            access_modes=["ReadWriteMany"],
            storage_class_name="efs-sc",
            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                requests={"storage": "5Gi"}
            ),
        ),
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
    edxapp_image = cached_image_uri(f"mitodl/edxapp@{EDXAPP_DOCKER_IMAGE_DIGEST}")
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
        image=cached_image_uri("busybox:1.35"),
        command=staticfiles_init_container_command,
        volume_mounts=staticfiles_volume_mounts,
    )

    # Setup the volume mount lists for the webapp and celery containers
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
        kubernetes.core.v1.VolumeMountArgs(
            name=configmaps.waffle_flags_yaml_config_name,
            mount_path="/openedx/config/waffle-flags.yaml",
            sub_path="waffle-flags.yaml",
        ),
        *staticfiles_volume_mounts,
    ]

    # The webapp deployment requires an addition mount of the uwsgi.ini configmap
    webapp_volume_mounts = [
        *common_volume_mounts,
        kubernetes.core.v1.VolumeMountArgs(
            name=configmaps.uwsgi_ini_config_name,
            mount_path="/openedx/edx-platform/uwsgi.ini",
            sub_path="uwsgi.ini",
        ),
    ]

    # Helper function to create the init container that aggregates the config files
    # helps reduce code duplication
    def _create_config_aggregator_init_container(
        service: str, volume_mounts: list[kubernetes.core.v1.VolumeMountArgs]
    ) -> kubernetes.core.v1.ContainerArgs:
        return kubernetes.core.v1.ContainerArgs(
            name="config-aggregator",
            image=cached_image_uri("busybox:1.35"),
            command=["/bin/sh", "-c"],
            args=[
                f"cat /openedx/config-sources/*/*.yaml > /openedx/config/{service}.env.yml"
            ],
            volume_mounts=volume_mounts,
        )

    # Helper function to create the affinity block for the deployments.
    # helps reduce code duplication
    def _create_affinity_args(
        match_labels: dict[str, str],
    ) -> kubernetes.core.v1.AffinityArgs:
        return kubernetes.core.v1.AffinityArgs(
            pod_anti_affinity=kubernetes.core.v1.PodAntiAffinityArgs(
                preferred_during_scheduling_ignored_during_execution=[
                    kubernetes.core.v1.WeightedPodAffinityTermArgs(
                        weight=100,
                        pod_affinity_term=kubernetes.core.v1.PodAffinityTermArgs(
                            label_selector=kubernetes.meta.v1.LabelSelectorArgs(
                                match_labels=match_labels,
                            ),
                            topology_key="kubernetes.io/hostname",
                        ),
                    ),
                ]
            )
        )

    # This function reduces code duplication when creating the pre-deploy migrate jobs
    def _create_pre_deploy_job(
        service_type: str,
        webapp_deployment_name: str,
        edxapp_volumes: list[kubernetes.core.v1.VolumeArgs],
        edxapp_init_volume_mounts: list[kubernetes.core.v1.VolumeMountArgs],
        command: list[str],
        args: list[str],
        purpose: str = "migrate",
        opts: ResourceOptions = None,
    ) -> kubernetes.batch.v1.Job:
        predeploy_labels = k8s_global_labels | {
            "ol.mit.edu/component": f"edxapp-{service_type}-predeploy",
            "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
        }
        return kubernetes.batch.v1.Job(
            (
                f"ol-{stack_info.env_prefix}-edxapp-{service_type}-predeploy-{purpose}-job-{stack_info.env_suffix}"
            ),
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                name=f"{webapp_deployment_name}-predeploy-{purpose}",
                namespace=namespace,
                labels=predeploy_labels,
            ),
            spec=kubernetes.batch.v1.JobSpecArgs(
                ttl_seconds_after_finished=60 * 30,  # 30 minutes
                template=kubernetes.core.v1.PodTemplateSpecArgs(
                    metadata=kubernetes.meta.v1.ObjectMetaArgs(
                        labels=predeploy_labels,
                    ),
                    spec=kubernetes.core.v1.PodSpecArgs(
                        service_account_name=vault_k8s_resources.service_account_name,
                        restart_policy="OnFailure",
                        volumes=edxapp_volumes,
                        init_containers=[
                            staticfiles_init_container,
                            _create_config_aggregator_init_container(
                                service_type, edxapp_init_volume_mounts
                            ),
                        ],
                        containers=[
                            kubernetes.core.v1.ContainerArgs(
                                name=f"{service_type}-edxapp-{purpose}",
                                image=edxapp_image,
                                command=command,
                                args=args,
                                env=[
                                    kubernetes.core.v1.EnvVarArgs(
                                        name="SERVICE_VARIANT", value=service_type
                                    ),
                                    kubernetes.core.v1.EnvVarArgs(
                                        name="DJANGO_SETTINGS_MODULE",
                                        value=f"{service_type}.envs.production",
                                    ),
                                ],
                                volume_mounts=common_volume_mounts,
                            )
                        ],
                    ),
                ),
            ),
            opts=opts,
        )

    ############################################
    # lms deployment resources
    ############################################
    # All of the secrets and configmaps that will be mounted into the edxapp lms containers
    # The names are prefixed with numbers to control the order they are concatenated in.
    lms_edxapp_config_sources = {
        secrets.db_creds_secret_name: secrets.db_creds,
        secrets.db_connections_secret_name: secrets.db_connections,
        secrets.mongo_db_creds_secret_name: secrets.mongo_db_creds,
        secrets.mongo_db_forum_secret_name: secrets.mongo_db_forum,
        secrets.general_secrets_name: secrets.general,
        secrets.forum_secret_name: secrets.forum,
        secrets.learn_ai_canvas_syllabus_token_secret_name: secrets.learn_ai_canvas_syllabus_token,
        configmaps.general_config_name: configmaps.general,
        configmaps.interpolated_config_name: configmaps.interpolated,
        # Just LMS specific resources below this line
        secrets.lms_oauth_secret_name: secrets.lms_oauth,
        configmaps.lms_general_config_name: configmaps.lms_general,
        configmaps.lms_interpolated_config_name: configmaps.lms_interpolated,
    }
    if secrets.xqueue_secret_name:
        lms_edxapp_config_sources[secrets.xqueue_secret_name] = secrets.xqueue
    lms_edxapp_secret_names = [
        secrets.db_creds_secret_name,
        secrets.db_connections_secret_name,
        secrets.mongo_db_creds_secret_name,
        secrets.mongo_db_forum_secret_name,
        secrets.general_secrets_name,
        secrets.forum_secret_name,
        secrets.learn_ai_canvas_syllabus_token_secret_name,
        # Just LMS specific resources below this line
        secrets.lms_oauth_secret_name,
    ]
    if secrets.xqueue_secret_name:
        lms_edxapp_secret_names.append(secrets.xqueue_secret_name)
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
    # The data volume is special and is shared between all app instances. Use an EFS PVC
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="openedx-data",
            persistent_volume_claim=kubernetes.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                claim_name=openedx_data_pvc.metadata.name
            ),
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
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name=configmaps.waffle_flags_yaml_config_name,
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=configmaps.waffle_flags_yaml_config_name,
            ),
        )
    )
    lms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="vector-config",
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=f"{env_name}-edxapp-vector-config",
            ),
        )
    )
    lms_edxapp_volumes.extend(staticfiles_volumes)

    # Define the volumemounts for the init container that aggregates the config files
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

    # Finally, actually define the LMS deployment
    # Start with a pre-deployment job that runs the migrations
    lms_pre_deploy_migrate_job = _create_pre_deploy_job(
        service_type="lms",
        webapp_deployment_name=lms_webapp_deployment_name,
        edxapp_volumes=lms_edxapp_volumes,
        edxapp_init_volume_mounts=lms_edxapp_init_volume_mounts,
        command=["python", "manage.py"],
        args=["lms", "migrate", "--noinput"],
        purpose="migrate",
        opts=ResourceOptions(depends_on=[*lms_edxapp_config_sources.values()]),
    )
    lms_pre_deploy_waffleflag_job = _create_pre_deploy_job(
        service_type="lms",
        webapp_deployment_name=lms_webapp_deployment_name,
        edxapp_volumes=lms_edxapp_volumes,
        edxapp_init_volume_mounts=lms_edxapp_init_volume_mounts,
        command=["python", "set_waffle_flags.py"],
        args=["/openedx/config/waffle-flags.yaml"],
        purpose="waffleflags",
        opts=ResourceOptions(depends_on=[*lms_edxapp_config_sources.values()]),
    )
    # It is important that the CMS and LMS deployment have distinct labels attached.
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
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=lms_webapp_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=lms_webapp_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    affinity=_create_affinity_args(lms_webapp_labels),
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,
                        # This init container will concatenate all the config files that come from
                        # the umpteen secrets and configmaps into a single file that edxapp expects
                        _create_config_aggregator_init_container(
                            "lms", lms_edxapp_init_volume_mounts
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="lms-edxapp",
                            image=edxapp_image,
                            command=["granian"],
                            args=[
                                "--interface",
                                "wsgi",
                                "--host",
                                "0.0.0.0",  # noqa: S104
                                "--port",
                                "8000",
                                "--workers",
                                "1",
                                "--log-level",
                                "warn",
                                "--static-path-mount",
                                "/openedx/staticfiles",
                                "lms.wsgi:application",
                            ],
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
                            resources=_get_resources_requests_limits("webapp", "lms"),
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    container_port=8000, name="http"
                                )
                            ],
                            volume_mounts=webapp_volume_mounts,
                        ),
                        kubernetes.core.v1.ContainerArgs(
                            name="vector",
                            image="timberio/vector:0.34.1-alpine",
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="ENVIRONMENT",
                                    value=stack_info.env_prefix,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="TIER",
                                    value=stack_info.env_suffix,
                                ),
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={
                                    "cpu": "100m",
                                    "memory": "128Mi",
                                },
                                limits={
                                    "memory": "256Mi",
                                },
                            ),
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-logs",
                                    mount_path="/opt/data/lms/logs",
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="vector-config",
                                    mount_path="/etc/vector",
                                    read_only=True,
                                ),
                            ],
                            args=["--config", "/etc/vector/vector.yaml"],
                        ),
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=[
                *lms_edxapp_config_sources.values(),
                lms_pre_deploy_migrate_job,
                lms_pre_deploy_waffleflag_job,
                vector_configmap,
            ]
        ),
    )
    lms_webapp_hpa = kubernetes.autoscaling.v2.HorizontalPodAutoscaler(
        f"ol-{stack_info.env_prefix}-edxapp-lms-hpa-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{lms_webapp_deployment_name}-hpa",
            namespace=namespace,
            labels=lms_webapp_labels,
        ),
        spec=kubernetes.autoscaling.v2.HorizontalPodAutoscalerSpecArgs(
            scale_target_ref=kubernetes.autoscaling.v2.CrossVersionObjectReferenceArgs(
                api_version="apps/v1",
                kind="Deployment",
                name=lms_webapp_deployment_name,
            ),
            min_replicas=replicas_dict["webapp"]["lms"]["min"],
            max_replicas=replicas_dict["webapp"]["lms"]["max"],
            metrics=webapp_hpa_scaling_metrics,
            behavior=webapp_hpa_behavior,
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

    # It is important that the celery workers have distinct labels from the webapps
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
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=lms_celery_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=lms_celery_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    affinity=_create_affinity_args(lms_celery_labels),
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,  # strictly speaking, not required
                        _create_config_aggregator_init_container(
                            "lms", lms_edxapp_init_volume_mounts
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
                                "--exclude-queues=edx.cms.core.default",
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
                            resources=_get_resources_requests_limits("celery", "lms"),
                            volume_mounts=common_volume_mounts,
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(depends_on=[*lms_edxapp_config_sources.values()]),
    )

    lms_celery_scaledobject = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-lms-celery-scaledobject-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="ScaledObject",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{lms_celery_deployment_name}-scaledobject",
            namespace=namespace,
            labels=lms_celery_labels,
        ),
        spec={
            "scaleTargetRef": {
                "kind": "Deployment",
                "name": lms_celery_deployment_name,
            },
            "pollingInterval": 3,
            "cooldownPeriod": 10,
            "minReplicaCount": replicas_dict["celery"]["lms"]["min"],
            "maxReplicaCount": replicas_dict["celery"]["lms"]["max"],
            "advanced": {
                "horizontalPodAutoscalerConfig": {
                    "behavior": {
                        "scaleUp": {"stabilizationWindowSeconds": 300},
                    }
                }
            },
            "triggers": [
                {
                    "type": "redis",
                    "metadata": {
                        "address": edxapp_cache.address.apply(
                            lambda addr: f"{addr}:{DEFAULT_REDIS_PORT}"
                        ),
                        "username": "default",
                        "databaseIndex": "1",
                        "password": edxapp_cache.cache_cluster.auth_token,
                        "listName": "edx.lms.core.default",
                        "listLength": "10",
                        "enableTLS": "true",
                    },
                }
            ],
        },
    )

    # Celery deployment do not require service definitions

    # Special one-off deployment that invokes the process_scheduled_emails.py script rather than launching the
    # application-proper.
    # It is important that this have distinct labels from the webapp or celery
    lms_process_scheduled_emails_labels = k8s_global_labels | {
        "ol.mit.edu/component": "edxapp-lms-process-scheduled-emails",
        "ol.mit.edu/pod-security-group": edxapp_k8s_app_security_group.id,
    }
    lms_process_scheduled_emails_deployment = kubernetes.apps.v1.Deployment(
        f"ol-{stack_info.env_prefix}-edxapp-lms-process-scheduled-emails-deployment-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{env_name}-edxapp-lms-process-scheduled-emails",
            namespace=namespace,
            labels=lms_process_scheduled_emails_labels,
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            replicas=1,
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=lms_process_scheduled_emails_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=lms_process_scheduled_emails_labels
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=lms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,
                        # This init container will concatenate all the config files that come from
                        # the umpteen secrets and configmaps into a single file that edxapp expects
                        _create_config_aggregator_init_container(
                            "lms", lms_edxapp_init_volume_mounts
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="lms-edxapp",
                            image=edxapp_image,
                            command=["python"],
                            args=[
                                "process_scheduled_emails.py",
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
                        ),
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=[
                *lms_edxapp_config_sources.values(),
                lms_pre_deploy_migrate_job,
                lms_pre_deploy_waffleflag_job,
            ]
        ),
    )

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
        secrets.forum_secret_name: secrets.forum,
        configmaps.general_config_name: configmaps.general,
        configmaps.interpolated_config_name: configmaps.interpolated,
        # Just CMS specific resources below this line
        secrets.cms_oauth_secret_name: secrets.cms_oauth,
        configmaps.cms_general_config_name: configmaps.cms_general,
        configmaps.cms_interpolated_config_name: configmaps.cms_interpolated,
    }
    if secrets.xqueue_secret_name:
        cms_edxapp_config_sources[secrets.xqueue_secret_name] = secrets.xqueue
    cms_edxapp_secret_names = [
        secrets.db_creds_secret_name,
        secrets.db_connections_secret_name,
        secrets.mongo_db_creds_secret_name,
        secrets.mongo_db_forum_secret_name,
        secrets.general_secrets_name,
        secrets.forum_secret_name,
        secrets.learn_ai_canvas_syllabus_token_secret_name,
        # Just CMS specific resources below this line
        secrets.cms_oauth_secret_name,
    ]
    if secrets.xqueue_secret_name:
        cms_edxapp_secret_names.append(secrets.xqueue_secret_name)
    cms_edxapp_configmap_names = [
        configmaps.general_config_name,
        configmaps.interpolated_config_name,
        # Just CMS specific resources below this line
        configmaps.cms_general_config_name,
        configmaps.cms_interpolated_config_name,
    ]

    # This can be confusing. We are working with two different 'volume' concepts here.
    # 1. The 'volumes' that are defined in the pod spec.
    # 2. The 'volumeMounts' that are defined in each container.
    # Not every container must have every (or any) volumeMounts, but the volumeMounts
    # it does have must be defined as volumes in the pod spec.
    #
    # volumeMounts reference volumes.
    #
    # See: https://kubernetes.io/docs/concepts/storage/volumes/

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
            persistent_volume_claim=kubernetes.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                claim_name=openedx_data_pvc.metadata.name
            ),
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
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name=configmaps.waffle_flags_yaml_config_name,
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=configmaps.waffle_flags_yaml_config_name,
            ),
        )
    )
    cms_edxapp_volumes.append(
        kubernetes.core.v1.VolumeArgs(
            name="vector-config",
            config_map=kubernetes.core.v1.ConfigMapVolumeSourceArgs(
                name=f"{env_name}-edxapp-vector-config",
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

    # Finally, actually define the CMS deployment
    # Start with a pre-deployment job that runs the migrations
    cms_pre_deploy_migrate_job = _create_pre_deploy_job(
        service_type="cms",
        webapp_deployment_name=cms_webapp_deployment_name,
        edxapp_volumes=cms_edxapp_volumes,
        edxapp_init_volume_mounts=cms_edxapp_init_volume_mounts,
        command=["python", "manage.py"],
        args=["cms", "migrate", "--noinput"],
        purpose="migrate",
        opts=ResourceOptions(
            depends_on=[*cms_edxapp_config_sources.values(), lms_pre_deploy_migrate_job]
        ),
    )
    # It is important that the CMS and LMS deployment have distinct labels attached.
    # These labels should be should be distict from those attached to the predeployment
    # jobs as well.
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
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=cms_webapp_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=cms_webapp_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    affinity=_create_affinity_args(cms_webapp_labels),
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=cms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,
                        # This init container will concatenate all the config files that come from
                        # the umpteen secrets and configmaps into a single file that edxapp expects
                        _create_config_aggregator_init_container(
                            "cms", cms_edxapp_init_volume_mounts
                        ),
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="cms-edxapp",
                            image=edxapp_image,
                            command=["granian"],
                            args=[
                                "--interface",
                                "wsgi",
                                "--host",
                                "0.0.0.0",  # noqa: S104
                                "--port",
                                "8000",
                                "--workers",
                                "1",
                                "--log-level",
                                "warn",
                                "--static-path-mount",
                                "/openedx/staticfiles",
                                "cms.wsgi:application",
                            ],
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
                            resources=_get_resources_requests_limits("webapp", "cms"),
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    container_port=8000, name="http"
                                )
                            ],
                            volume_mounts=webapp_volume_mounts,
                        ),
                        kubernetes.core.v1.ContainerArgs(
                            name="vector",
                            image="timberio/vector:0.34.1-alpine",
                            env=[
                                kubernetes.core.v1.EnvVarArgs(
                                    name="ENVIRONMENT",
                                    value=stack_info.env_prefix,
                                ),
                                kubernetes.core.v1.EnvVarArgs(
                                    name="TIER",
                                    value=stack_info.env_suffix,
                                ),
                            ],
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={
                                    "cpu": "100m",
                                    "memory": "128Mi",
                                },
                                limits={
                                    "memory": "256Mi",
                                },
                            ),
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="openedx-logs",
                                    mount_path="/opt/data/cms/logs",
                                    read_only=True,
                                ),
                                kubernetes.core.v1.VolumeMountArgs(
                                    name="vector-config",
                                    mount_path="/etc/vector",
                                    read_only=True,
                                ),
                            ],
                            args=["--config", "/etc/vector/vector.yaml"],
                        ),
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(
            depends_on=[
                *cms_edxapp_config_sources.values(),
                cms_pre_deploy_migrate_job,
                vector_configmap,
            ]
        ),
    )
    cms_webapp_hpa = kubernetes.autoscaling.v2.HorizontalPodAutoscaler(
        f"ol-{stack_info.env_prefix}-edxapp-cms-hpa-{stack_info.env_suffix}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{cms_webapp_deployment_name}-hpa",
            namespace=namespace,
            labels=cms_webapp_labels,
        ),
        spec=kubernetes.autoscaling.v2.HorizontalPodAutoscalerSpecArgs(
            scale_target_ref=kubernetes.autoscaling.v2.CrossVersionObjectReferenceArgs(
                api_version="apps/v1",
                kind="Deployment",
                name=cms_webapp_deployment_name,
            ),
            min_replicas=replicas_dict["webapp"]["cms"]["min"],
            max_replicas=replicas_dict["webapp"]["cms"]["max"],
            metrics=webapp_hpa_scaling_metrics,
            behavior=webapp_hpa_behavior,
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

    # It is important that the celery workers have distinct labels from the webapps
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
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=cms_celery_labels
            ),
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(labels=cms_celery_labels),
                spec=kubernetes.core.v1.PodSpecArgs(
                    affinity=_create_affinity_args(cms_celery_labels),
                    service_account_name=vault_k8s_resources.service_account_name,
                    volumes=cms_edxapp_volumes,
                    init_containers=[
                        staticfiles_init_container,  # strictly speaking, not required
                        _create_config_aggregator_init_container(
                            "cms", cms_edxapp_init_volume_mounts
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
                            resources=_get_resources_requests_limits("celery", "cms"),
                            volume_mounts=common_volume_mounts,
                        )
                    ],
                ),
            ),
        ),
        opts=pulumi.ResourceOptions(depends_on=[*cms_edxapp_config_sources.values()]),
    )
    cms_celery_scaledobject = kubernetes.apiextensions.CustomResource(
        f"ol-{stack_info.env_prefix}-edxapp-cms-celery-scaledobject-{stack_info.env_suffix}",
        api_version="keda.sh/v1alpha1",
        kind="ScaledObject",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"{cms_celery_deployment_name}-scaledobject",
            namespace=namespace,
            labels=cms_celery_labels,
        ),
        spec={
            "scaleTargetRef": {
                "kind": "Deployment",
                "name": cms_celery_deployment_name,
            },
            "pollingInterval": 3,
            "cooldownPeriod": 10,
            "minReplicaCount": replicas_dict["celery"]["cms"]["min"],
            "maxReplicaCount": replicas_dict["celery"]["cms"]["max"],
            "triggers": [
                {
                    "type": "redis",
                    "metadata": {
                        "address": edxapp_cache.address.apply(
                            lambda addr: f"{addr}:{DEFAULT_REDIS_PORT}"
                        ),
                        "username": "default",
                        "databaseIndex": "1",
                        "password": edxapp_cache.cache_cluster.auth_token,
                        "listName": "edx.cms.core.default",
                        "listLength": "10",
                        "enableTLS": "true",
                    },
                }
            ],
        },
    )

    # APISIX ingress configuration and setup
    apisix_ingress_class = edxapp_config.get("apisix_ingress_class") or "apisix"
    tls_secret_name = (
        "shared-backend-tls-pair"  # pragma: allowlist secret  # noqa: S105
    )
    cert_manager_certificate = OLCertManagerCert(
        f"ol-{stack_info.env_prefix}-edxapp-tls-cert-{stack_info.env_suffix}",
        cert_config=OLCertManagerCertConfig(
            application_name="edxapp",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            create_apisixtls_resource=True,
            dest_secret_name=tls_secret_name,
            dns_names=[
                edxapp_config.require("backend_lms_domain"),
                edxapp_config.require("backend_studio_domain"),
                edxapp_config.require("backend_preview_domain"),
            ],
        ),
    )

    edxapp_shared_plugins = OLApisixSharedPlugins(
        f"ol-{stack_info.env_prefix}-edxapp-shared-plugins-{stack_info.env_suffix}",
        plugin_config=OLApisixSharedPluginsConfig(
            application_name="edxapp",
            resource_suffix="ol-shared-plugins",
            k8s_namespace=namespace,
            k8s_labels=k8s_global_labels,
            enable_defaults=True,
        ),
    )

    lms_apisixroute = OLApisixRoute(
        name=f"ol-{stack_info.env_prefix}-edxapp-lms-apisix-route-{stack_info.env_suffix}",
        k8s_namespace=namespace,
        k8s_labels=k8s_global_labels,
        route_configs=[
            OLApisixRouteConfig(
                route_name="lms-default",
                priority=0,
                plugins=[],
                shared_plugins=edxapp_shared_plugins.resource_name,
                hosts=[
                    edxapp_config.require("backend_lms_domain"),
                    edxapp_config.require_object("domains")["lms"],
                ],
                paths=["/*"],
                backend_service_name=lms_webapp_deployment_name,
                backend_service_port="http",
            ),
            OLApisixRouteConfig(
                route_name="cms-default",
                priority=0,
                plugins=[],
                shared_plugins=edxapp_shared_plugins.resource_name,
                hosts=[
                    edxapp_config.require("backend_studio_domain"),
                    edxapp_config.require_object("domains")["studio"],
                ],
                paths=["/*"],
                backend_service_name=cms_webapp_deployment_name,
                backend_service_port="http",
            ),
        ],
    )

    return {
        "edxapp_k8s_app_security_group_id": edxapp_k8s_app_security_group.id,
    }
