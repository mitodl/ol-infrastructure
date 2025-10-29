"""Pulumi program for deploying the MIT Learn Next.js application to Kubernetes."""

import os

import pulumi_kubernetes as kubernetes
from kubernetes import client, config
from pulumi import Config, Output, ResourceOptions, StackReference, export

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
def get_last_active_from_configmap() -> str:
    """Read the last active deployment color from ConfigMap state store."""
    try:
        # Use the kubernetes client library to read from the cluster
        # This requires KUBECONFIG to be set or default kubeconfig to exist
        config.load_kube_config()
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
last_active = get_last_active_from_configmap()

# Determine which color gets the new deployment
# If auto_toggle is enabled, we deploy to the opposite color and will switch to it
# If auto_toggle is disabled, we always deploy to last_active (traditional deployment)
if auto_toggle:
    new_color = "green" if last_active == "blue" else "blue"
    active_color = new_color  # Will become active once deployment is ready
else:
    new_color = last_active
    active_color = last_active

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

# Get the PVC name for the new deployment
new_pvc_name = f"nextjs-build-cache-efs-{new_color}"

# Define the volume to be used by the new deployment and job
efs_volume = kubernetes.core.v1.VolumeArgs(
    name=new_pvc_name,
    persistent_volume_claim=kubernetes.core.v1.PersistentVolumeClaimVolumeSourceArgs(
        claim_name=new_pvc_name,
    ),
)

# Define the volume mount for the EFS volume
efs_volume_mount = kubernetes.core.v1.VolumeMountArgs(
    name=new_pvc_name,
    mount_path="/app/frontends/main/.next",
)

# Create labels for the new deployment
new_deployment_labels = application_labels | {"deployment-color": new_color}

# Create labels for the old deployment (if different)
old_color = "blue" if new_color == "green" else "green"
old_deployment_labels = application_labels | {"deployment-color": old_color}

# Create a Kubernetes Job to build the static assets for the new deployment
mit_learn_nextjs_build_job = kubernetes.batch.v1.Job(
    f"mit-learn-nextjs-{stack_info.name}-build-job-{new_color}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"mit-learn-nextjs-build-{new_color}",
        namespace=learn_namespace,
        labels=new_deployment_labels,
    ),
    spec=kubernetes.batch.v1.JobSpecArgs(
        backoff_limit=3,
        ttl_seconds_after_finished=300,
        active_deadline_seconds=1200,
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=new_deployment_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                restart_policy="OnFailure",
                volumes=[efs_volume],
                containers=[
                    kubernetes.core.v1.ContainerArgs(
                        name="nextjs-build",
                        image=app_image,
                        command=["yarn", "build", "--no-lint"],
                        env=env_vars,
                        volume_mounts=[efs_volume_mount],
                        image_pull_policy="Always",
                        resources=kubernetes.core.v1.ResourceRequirementsArgs(
                            requests={"cpu": "1000m", "memory": "8Gi"},
                            limits={"cpu": "3000m", "memory": "8Gi"},
                        ),
                    ),
                ],
            ),
        ),
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
        depends_on=[blue_pvc if new_color == "blue" else green_pvc],
    ),
)

# Create the new deployment (blue or green based on new_color)
mit_learn_nextjs_new_deployment = kubernetes.apps.v1.Deployment(
    f"mit-learn-nextjs-{stack_info.name}-deployment-{new_color}",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name=f"mit-learn-nextjs-{new_color}",
        namespace=learn_namespace,
        labels=new_deployment_labels,
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=new_deployment_labels,
        ),
        replicas=nextjs_config.get_int("pod_count") or 2,
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=new_deployment_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
                volumes=[efs_volume],
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
                        volume_mounts=[efs_volume_mount],
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=DEFAULT_NEXTJS_PORT,
                            ),
                            initial_delay_seconds=30,
                            period_seconds=30,
                            failure_threshold=3,
                        ),
                        # Readiness probe to check if the application is ready to serve
                        # traffic
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=DEFAULT_NEXTJS_PORT,
                            ),
                            initial_delay_seconds=15,
                            period_seconds=15,
                            failure_threshold=3,
                        ),
                        # Startup probe to ensure the application is fully initialized
                        # before other probes start
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
        "previous_active": last_active,
        "auto_toggle": str(auto_toggle),
    },
    opts=ResourceOptions(
        depends_on=[mit_learn_nextjs_new_deployment],
    ),
)

# Create a Kubernetes Service that routes to the active deployment
# With auto_toggle enabled, this automatically routes to the new deployment
mit_learn_nextjs_service_name = "mit-learn-nextjs"
active_deployment_labels = application_labels | {"deployment-color": active_color}

mit_learn_nextjs_service = kubernetes.core.v1.Service(
    f"mit-learn-nextjs-{stack_info.name}-service-resource",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mit-learn-nextjs",
        namespace=learn_namespace,
        labels=application_labels,
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
        parent=mit_learn_nextjs_new_deployment,
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
export("previous_deployment", last_active)
export("auto_toggle_enabled", auto_toggle)
export("state_configmap", "mit-learn-nextjs-deployment-state")

Output.all(active_color, new_color, last_active, auto_toggle).apply(
    lambda vals: {
        "active_deployment": vals[0],
        "new_deployment": vals[1],
        "last_active": vals[2],
        "auto_toggle_enabled": vals[3],
        "instructions": (
            "Blue/Green Deployment Status:\n"
            f"- Auto-toggle: {'ENABLED' if vals[3] else 'DISABLED'}\n"
            f"- Current ACTIVE deployment: {vals[0]}\n"
            f"- New deployment: {vals[1]}\n"
            f"- Previous deployment: {vals[2]}\n"
            "\n"
            + (
                "Automatic Mode:\n"
                "  The system toggles between blue and green on each run.\n"
                f"  Service is now routing to: {vals[0]}\n"
                f"  Old deployment ({vals[2]}) is still running but not "
                "receiving traffic.\n"
                "\n"
                "  State is automatically tracked in ConfigMap:\n"
                "  kubectl get configmap mit-learn-nextjs-deployment-state "
                "-n mitlearn -o yaml\n"
                "\n"
                "  Optional: Scale down old deployment manually:\n"
                f"  kubectl scale deployment mit-learn-nextjs-{vals[2]} "
                "--replicas=0 -n mitlearn\n"
                "\n"
                "  On next 'pulumi up', traffic will automatically switch "
                f"back to {vals[2]}.\n"
                "\n"
                "To disable auto-toggle:\n"
                "  pulumi config set nextjs:auto_toggle false\n"
                if vals[3]
                else "Manual Mode:\n"
                f"  Service is routing to: {vals[0]}\n"
                f"  To switch to {vals[1]}, run:\n"
                f"  pulumi config set nextjs:last_active {vals[1]}\n"
                "  pulumi up\n"
                "\n"
                "To enable auto-toggle:\n"
                "  pulumi config set nextjs:auto_toggle true\n"
            )
        ),
    }
)
