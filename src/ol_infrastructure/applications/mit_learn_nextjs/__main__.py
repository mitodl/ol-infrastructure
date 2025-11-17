"""Pulumi program for deploying the MIT Learn Next.js application to Kubernetes."""

import os
from typing import Any

import pulumi_kubernetes as kubernetes
from kubernetes import client, config
from pulumi import Config, ResourceOptions, StackReference, export

from bridge.lib.magic_numbers import DEFAULT_NEXTJS_PORT
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    ecr_image_uri,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import BusinessUnit, K8sGlobalLabels, Services
from ol_infrastructure.lib.pulumi_helper import parse_stack

stack_info = parse_stack()

cluster_stack = StackReference(f"infrastructure.aws.eks.applications.{stack_info.name}")
# Assume the application image URI comes from a separate image build stack
if "MIT_LEARN_NEXTJS_DOCKER_TAG" not in os.environ:
    msg = "MIT_LEARN_NEXTJS_DOCKER_TAG environment varibale must be set."
    raise OSError(msg)
MIT_LEARN_NEXTJS_DOCKER_TAG = os.environ["MIT_LEARN_NEXTJS_DOCKER_TAG"]

app_image = ecr_image_uri(f"mitodl/mit-learn-nextjs-app:{MIT_LEARN_NEXTJS_DOCKER_TAG}")

k8s_global_labels = K8sGlobalLabels(
    service=Services.mit_learn, ou=BusinessUnit.mit_learn, stack=stack_info
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

learn_namespace = "mitlearn"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(learn_namespace, ns)
)

nextjs_config = Config("nextjs")

# Blue/Green deployment configuration
# The system automatically toggles between blue and green on each deployment
# The new version is deployed to the inactive color, validated, then activated
auto_toggle = nextjs_config.get_bool("auto_toggle")
if auto_toggle is None:
    auto_toggle = True  # Default to automatic toggling


# Function to read last_active from ConfigMap in the cluster
def get_last_active_from_configmap(kube_config_dict: dict[str, Any]) -> str:
    """Read the last active deployment color from ConfigMap state store."""
    try:
        # Use the kubernetes client library to read from the cluster
        # Load kubeconfig from the provided dictionary
        config.load_kube_config_from_dict(kube_config_dict)
        v1 = client.CoreV1Api()

        try:
            cm = v1.read_namespaced_config_map(
                name="mit-learn-nextjs-deployment-state",
                namespace=learn_namespace,
            )
            return cm.data.get("last_active", "blue")
        except client.exceptions.ApiException as e:
            NOT_FOUND = 404
            if e.status == NOT_FOUND:
                # ConfigMap doesn't exist yet (first run), default to blue
                return "blue"
            # For other API errors, return default
            return "blue"
    except (OSError, ValueError):
        # If we can't connect to cluster (no kubeconfig, etc), default to blue
        return "blue"


# Try to read last_active from ConfigMap, fall back to blue for first deployment
# This is done asynchronously via Output.apply() to handle Pulumi's async nature
last_active = cluster_stack.require_output("kube_config").apply(
    get_last_active_from_configmap
)


# Determine which color gets the new deployment
# If auto_toggle is enabled, we deploy to the opposite color and will switch to it
# If auto_toggle is disabled, we always deploy to last_active (traditional deployment)
def determine_colors(last_active_color: str) -> dict[str, str]:
    """Determine new_color and active_color based on auto_toggle setting."""
    if auto_toggle:
        new = "green" if last_active_color == "blue" else "blue"
        return {
            "new_color": new,
            "active_color": new,
            "last_active": last_active_color,
        }
    else:
        return {
            "new_color": last_active_color,
            "active_color": last_active_color,
            "last_active": last_active_color,
        }


colors = last_active.apply(determine_colors)
new_color = colors.apply(lambda c: c["new_color"])
active_color = colors.apply(lambda c: c["active_color"])
last_active_resolved = colors.apply(lambda c: c["last_active"])

raw_env_vars = {
    # Env vars available only on server
    "MITOL_NOINDEX": nextjs_config.get("mitol_noindex"),
    "NEXT_PUBLIC_OPTIMIZE_IMAGES": nextjs_config.get("optimize_images"),
    # Env vars available on client and server
    "NEXT_PUBLIC_APPZI_URL": nextjs_config.require("appzi_url"),
    "NEXT_PUBLIC_CSRF_COOKIE_NAME": nextjs_config.require("csrf_cookie_name"),
    "NEXT_PUBLIC_EMBEDLY_KEY": nextjs_config.require("embedly_key"),
    "NEXT_PUBLIC_LEARN_AI_RECOMMENDATION_ENDPOINT": nextjs_config.require(
        "recommendation_endpoint"
    ),
    "NEXT_PUBLIC_LEARN_AI_SYLLABUS_ENDPOINT": nextjs_config.require(
        "syllabus_endpoint"
    ),
    "NEXT_PUBLIC_MITOL_API_BASE_URL": nextjs_config.require("mitlearn_api_base_url"),
    "NEXT_PUBLIC_MITX_ONLINE_CSRF_COOKIE_NAME": "csrf_mitxonline",
    "NEXT_PUBLIC_MITX_ONLINE_BASE_URL": nextjs_config.require("mitxonline_base_url"),
    "NEXT_PUBLIC_MITOL_AXIOS_WITH_CREDENTIALS": "true",
    "NEXT_PUBLIC_MITOL_SUPPORT_EMAIL": "mitlearn-support@mit.edu",
    "NEXT_PUBLIC_ORIGIN": nextjs_config.require("origin"),
    "NEXT_PUBLIC_POSTHOG_API_HOST": nextjs_config.require("posthog_api_host"),
    "NEXT_PUBLIC_POSTHOG_API_KEY": nextjs_config.require("posthog_api_key"),
    "NEXT_PUBLIC_POSTHOG_PROJECT_ID": nextjs_config.require("posthog_project_id"),
    "NEXT_PUBLIC_SENTRY_DSN": nextjs_config.require("sentry_dsn"),
    "NEXT_PUBLIC_SENTRY_ENV": nextjs_config.require("sentry_env"),
    "NEXT_PUBLIC_SENTRY_PROFILES_SAMPLE_RATE": "0.25",
    "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE": "0.25",
    "NEXT_PUBLIC_SITE_NAME": "MIT Learn",
    "NEXT_PUBLIC_VERSION": MIT_LEARN_NEXTJS_DOCKER_TAG,
    "NEXT_PUBLIC_FEATURE_product_page_courses": False,
    "NEXT_PUBLIC_FEATURE_enrollment_dashboard": False,  # pragma: allowlist secret
    "NEXT_PUBLIC_FEATURE_lr_drawer_chatbot": True,
    "NEXT_PUBLIC_FEATURE_home_page_recommendation_bot": True,  # pragma: allowlist secret  # noqa: E501
}

env_vars = []
for k, v in raw_env_vars.items():
    env_vars.append(
        kubernetes.core.v1.EnvVarArgs(
            name=k,
            value=v,
        )
    )

application_labels = k8s_global_labels | {
    "ol.mit.edu/service": "nextjs",
}


# Create separate PVCs for blue and green deployments
def create_pvc_for_color(color: str) -> kubernetes.core.v1.PersistentVolumeClaim:
    """Create a PVC for the specified color deployment."""
    pvc_name = f"nextjs-build-cache-efs-{color}"
    color_labels = application_labels | {"deployment-color": color}
    return kubernetes.core.v1.PersistentVolumeClaim(
        f"mit-learn-nextjs-{stack_info.name}-pvc-{color}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=pvc_name,
            namespace=learn_namespace,
            labels=color_labels,
        ),
        spec=kubernetes.core.v1.PersistentVolumeClaimSpecArgs(
            access_modes=["ReadWriteMany"],
            resources=kubernetes.core.v1.VolumeResourceRequirementsArgs(
                requests={"storage": "10Gi"},
            ),
            storage_class_name="efs-sc",
        ),
    )


# Create PVCs for both blue and green
blue_pvc = create_pvc_for_color("blue")
green_pvc = create_pvc_for_color("green")

# Create a Kubernetes Job to build the static assets for the new deployment
mit_learn_nextjs_build_job = kubernetes.batch.v1.Job(
    f"mit-learn-nextjs-{stack_info.name}-build-job",
    metadata=new_color.apply(
        lambda c: kubernetes.meta.v1.ObjectMetaArgs(
            name=f"mit-learn-nextjs-build-{c}",
            namespace=learn_namespace,
            labels=application_labels | {"deployment-color": c},
        )
    ),
    spec=new_color.apply(
        lambda c: kubernetes.batch.v1.JobSpecArgs(
            backoff_limit=3,
            ttl_seconds_after_finished=300,
            active_deadline_seconds=1200,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=application_labels | {"deployment-color": c}
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    restart_policy="OnFailure",
                    volumes=[
                        kubernetes.core.v1.VolumeArgs(
                            name=f"nextjs-build-cache-efs-{c}",
                            persistent_volume_claim=kubernetes.core.v1.PersistentVolumeClaimVolumeSourceArgs(
                                claim_name=f"nextjs-build-cache-efs-{c}",
                            ),
                        )
                    ],
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="nextjs-build",
                            image=app_image,
                            command=["yarn", "build", "--no-lint"],
                            env=env_vars,
                            volume_mounts=[
                                kubernetes.core.v1.VolumeMountArgs(
                                    name=f"nextjs-build-cache-efs-{c}",
                                    mount_path="/app/frontends/main/.next",
                                )
                            ],
                            image_pull_policy="Always",
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "1000m", "memory": "8Gi"},
                                limits={"cpu": "3000m", "memory": "8Gi"},
                            ),
                        ),
                    ],
                ),
            ),
        )
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[blue_pvc, green_pvc],
    ),
)


# Helper function to create a deployment for a specific color
def create_deployment_for_color(color: str) -> kubernetes.apps.v1.Deployment:
    """Create a blue or green deployment."""
    color_labels = application_labels | {"deployment-color": color}
    pvc_name = f"nextjs-build-cache-efs-{color}"

    volume = kubernetes.core.v1.VolumeArgs(
        name=pvc_name,
        persistent_volume_claim=kubernetes.core.v1.PersistentVolumeClaimVolumeSourceArgs(
            claim_name=pvc_name,
        ),
    )

    volume_mount = kubernetes.core.v1.VolumeMountArgs(
        name=pvc_name,
        mount_path="/app/frontends/main/.next",
    )

    # Determine replica count: active deployment gets configured count, inactive gets 0
    def get_replicas(active: str) -> int:
        if color == active:
            return nextjs_config.get_int("pod_count") or 2
        return 0

    # Determine the value for the skipAwait annotation
    def get_skip_await_annotation(active: str) -> bool:
        return color != active

    replicas = active_color.apply(get_replicas)
    skip_await_annotation = active_color.apply(get_skip_await_annotation)

    return kubernetes.apps.v1.Deployment(
        f"mit-learn-nextjs-{stack_info.name}-deployment-{color}",
        metadata=kubernetes.meta.v1.ObjectMetaArgs(
            name=f"mit-learn-nextjs-{color}",
            namespace=learn_namespace,
            labels=color_labels,
            annotations={
                "deployment.kubernetes.io/description": (
                    f"Blue/green deployment for MIT Learn Next.js ({color})"
                ),
                "pulumi.com/skipAwait": skip_await_annotation.apply(
                    lambda skip: "true" if skip else "false"
                ),
            },
        ),
        spec=kubernetes.apps.v1.DeploymentSpecArgs(
            selector=kubernetes.meta.v1.LabelSelectorArgs(
                match_labels=color_labels,
            ),
            replicas=replicas,
            template=kubernetes.core.v1.PodTemplateSpecArgs(
                metadata=kubernetes.meta.v1.ObjectMetaArgs(
                    labels=color_labels,
                ),
                spec=kubernetes.core.v1.PodSpecArgs(
                    volumes=[volume],
                    dns_policy="ClusterFirst",
                    containers=[
                        kubernetes.core.v1.ContainerArgs(
                            name="nextjs-app",
                            image=app_image,
                            ports=[
                                kubernetes.core.v1.ContainerPortArgs(
                                    container_port=DEFAULT_NEXTJS_PORT,
                                    name="http",
                                )
                            ],
                            image_pull_policy="Always",
                            resources=kubernetes.core.v1.ResourceRequirementsArgs(
                                requests={"cpu": "250m", "memory": "500Mi"},
                                limits={"cpu": "1000m", "memory": "2Gi"},
                            ),
                            env=env_vars,
                            volume_mounts=[volume_mount],
                            liveness_probe=kubernetes.core.v1.ProbeArgs(
                                tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                    port=DEFAULT_NEXTJS_PORT,
                                ),
                                initial_delay_seconds=30,
                                period_seconds=30,
                                failure_threshold=3,
                            ),
                            readiness_probe=kubernetes.core.v1.ProbeArgs(
                                tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                    port=DEFAULT_NEXTJS_PORT,
                                ),
                                initial_delay_seconds=15,
                                period_seconds=15,
                                failure_threshold=3,
                            ),
                            startup_probe=kubernetes.core.v1.ProbeArgs(
                                tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                    port=DEFAULT_NEXTJS_PORT,
                                ),
                                initial_delay_seconds=10,
                                period_seconds=10,
                                failure_threshold=30,
                                success_threshold=1,
                                timeout_seconds=5,
                            ),
                        ),
                    ],
                ),
            ),
        ),
        opts=ResourceOptions(
            delete_before_replace=True,
            depends_on=[mit_learn_nextjs_build_job],
        ),
    )


# Create both blue and green deployments
blue_deployment = create_deployment_for_color("blue")
green_deployment = create_deployment_for_color("green")

# In blue/green deployment, we leave the old deployment running.
# Traffic is switched by updating the service selector to point to active_color.
# The old deployment can be manually scaled down or deleted after
# validating the new one.

# Create/Update ConfigMap to track deployment state
# This ConfigMap stores which deployment color is currently active
deployment_state_configmap = kubernetes.core.v1.ConfigMap(
    f"mit-learn-nextjs-{stack_info.name}-deployment-state",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mit-learn-nextjs-deployment-state",
        namespace=learn_namespace,
        labels=application_labels,
    ),
    data={
        "last_active": active_color,
        "previous_active": last_active_resolved,
        "auto_toggle": str(auto_toggle),
    },
    opts=ResourceOptions(
        depends_on=[blue_deployment, green_deployment],
    ),
)

# Create a Kubernetes Service that routes to the active deployment
# With auto_toggle enabled, this automatically routes to the new deployment
mit_learn_nextjs_service_name = "mit-learn-nextjs"
active_deployment_labels = active_color.apply(
    lambda color: application_labels | {"deployment-color": color}
)

mit_learn_nextjs_service = kubernetes.core.v1.Service(
    f"mit-learn-nextjs-{stack_info.name}-service-resource",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mit-learn-nextjs",
        namespace=learn_namespace,
        labels=application_labels,
        annotations={"pulumi.com/patchForce": "true"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        selector=active_deployment_labels,
        ports=[
            kubernetes.core.v1.ServicePortArgs(
                port=DEFAULT_NEXTJS_PORT,
                target_port=DEFAULT_NEXTJS_PORT,
                protocol="TCP",
                name="http",
            )
        ],
        type="ClusterIP",
    ),
    opts=ResourceOptions(
        depends_on=[blue_deployment, green_deployment],
    ),
)

gateway = OLEKSGateway(
    f"mit-learn-nextjs-{stack_info.name}-gateway",
    gateway_config=OLEKSGatewayConfig(
        cert_issuer="letsencrypt-production",
        cert_issuer_class="cluster-issuer",
        gateway_name="mit-learn-nextjs-gateway",
        labels=k8s_global_labels,
        namespace=learn_namespace,
        listeners=[
            OLEKSGatewayListenerConfig(
                name="https",
                hostname=nextjs_config.require("domain"),
                port=8443,
                tls_mode="Terminate",
                certificate_secret_name="mit-learn-nextjs-tls",  # noqa: S106  # pragma: allowlist secret
                certificate_secret_namespace=learn_namespace,
            ),
        ],
        routes=[
            OLEKSGatewayRouteConfig(
                backend_service_name=mit_learn_nextjs_service_name,
                backend_service_namespace=learn_namespace,
                backend_service_port=DEFAULT_NEXTJS_PORT,
                hostnames=[nextjs_config.require("domain")],
                name="mit-learn-nextjs-https",
                listener_name="https",
                port=8443,
            ),
        ],
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

# Export deployment information
# State is automatically tracked in the ConfigMap, no manual updates needed
export("current_active_deployment", active_color)
export("previous_deployment", last_active_resolved)
export("auto_toggle_enabled", auto_toggle)
export("state_configmap", "mit-learn-nextjs-deployment-state")
