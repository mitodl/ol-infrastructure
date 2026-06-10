"""Pulumi program for deploying the MIT Learn Next.js application to Kubernetes."""

import pulumi_kubernetes as kubernetes
from kubernetes.utils.quantity import parse_quantity
from pulumi import Config, ResourceOptions, export

from bridge.lib.magic_numbers import DEFAULT_NEXTJS_PORT
from ol_infrastructure.components.aws.eks import (
    OLEKSGateway,
    OLEKSGatewayConfig,
    OLEKSGatewayListenerConfig,
    OLEKSGatewayRateLimitConfig,
    OLEKSGatewayRouteConfig,
)
from ol_infrastructure.lib import pulumi_projects as projects
from ol_infrastructure.lib.aws.eks_helper import (
    check_cluster_namespace,
    ecr_image_uri,
    setup_k8s_provider,
)
from ol_infrastructure.lib.ol_types import (
    Application,
    BusinessUnit,
    K8sAppLabels,
    Product,
    Services,
)
from ol_infrastructure.lib.pulumi_helper import (
    format_docker_image_ref,
    get_docker_image_tag,
    make_stack_reference,
    merge_otel_resource_attributes,
    parse_stack,
)

stack_info = parse_stack()

cluster_stack = make_stack_reference(projects.EKS, f"applications.{stack_info.name}")
MIT_LEARN_NEXTJS_DOCKER_TAG = get_docker_image_tag("MIT_LEARN_NEXTJS")

app_image = ecr_image_uri(
    format_docker_image_ref("mitodl/mit-learn-nextjs-app", "MIT_LEARN_NEXTJS")
)

k8s_app_labels = K8sAppLabels(
    product=Product.mitlearn,
    service=Services.mit_learn,
    application=Application.mit_learn,
    component="frontend",
    ou=BusinessUnit.mit_learn,
    source_repository="https://github.com/mitodl/mit-learn",
    stack=stack_info,
).model_dump()

setup_k8s_provider(kubeconfig=cluster_stack.require_output("kube_config"))

learn_namespace = "mitlearn"
cluster_stack.require_output("namespaces").apply(
    lambda ns: check_cluster_namespace(learn_namespace, ns)
)

nextjs_config = Config("nextjs")

# Node (v24) auto-sizes its default heap ceiling from the container's cgroup memory
# limit, but production logs show it consistently landing around ~500MiB and crashing
# with "FATAL ERROR: ... JavaScript heap out of memory" (exit 139) roughly every 90
# minutes per pod under normal, steady request volume -- well short of the 1Gi this
# container is actually allowed. Setting --max-old-space-size explicitly makes V8 use
# the memory it's already been granted instead of self-limiting to a much lower
# default. NEXTJS_NON_HEAP_OVERHEAD_MIB reserves headroom within the same limit for
# non-heap V8/Node needs (code cache, native buffers, thread stacks) that sit outside
# the old-space budget.
#
# This is a fixed MiB amount rather than a percentage of the limit on purpose:
# non-heap overhead is driven by concurrency/workload shape, not by how much memory
# the container happens to have, so it doesn't scale with the limit. A fixed reserve
# means raising nextjs_memory_limit later hands all of the added memory straight to
# the heap (the actual thing that needs more room); a percentage would keep skimming
# an ever-larger, unneeded slice off the top as the limit grows.
nextjs_memory_limit = "1Gi"
NEXTJS_NON_HEAP_OVERHEAD_MIB = 224
nextjs_max_old_space_size_mib = (
    int(parse_quantity(nextjs_memory_limit)) // (1024 * 1024)
    - NEXTJS_NON_HEAP_OVERHEAD_MIB
)

stay_updated_hubspot_form_ids = {
    "ci": "f201f3af-c2c0-4b7d-b297-ddbb75912cc1",
    "qa": "f201f3af-c2c0-4b7d-b297-ddbb75912cc1",
    "production": "a5d18493-dcdb-4482-ad10-16ab66a35526",
}

try:
    stay_updated_hubspot_form_id = stay_updated_hubspot_form_ids[stack_info.env_suffix]
except KeyError as exc:
    msg = f"Unsupported MIT Learn Next.js environment: {stack_info.env_suffix}"
    raise ValueError(msg) from exc

raw_env_vars = {
    # Env vars available only on server
    "NODE_OPTIONS": f"--max-old-space-size={nextjs_max_old_space_size_mib}",
    "MITOL_NOINDEX": nextjs_config.get("mitol_noindex"),
    "NEXT_PUBLIC_OPTIMIZE_IMAGES": nextjs_config.get("optimize_images"),
    "GTM_TRACKING_ID": nextjs_config.get("gtm_tracking_id") or "",
    "GTM_AUTH": nextjs_config.get("gtm_auth") or "",
    "GTM_PREVIEW": nextjs_config.get("gtm_preview") or "",
    "GTM_COOKIES_WIN": nextjs_config.get("gtm_cookies_win") or "",
    "NEXT_CACHE_S_MAXAGE_SECONDS": nextjs_config.get("cache_s_maxage_seconds") or "",
    # Env vars available on client and server
    "NEXT_PUBLIC_APPZI_URL": nextjs_config.require("appzi_url"),
    "NEXT_PUBLIC_CSRF_COOKIE_NAME": nextjs_config.require("csrf_cookie_name"),
    "NEXT_PUBLIC_EMBEDLY_KEY": nextjs_config.require("embedly_key"),
    "NEXT_PUBLIC_LEARN_AI_CSRF_COOKIE_NAME": f"learn_ai_{stack_info.env_suffix}_csrftoken".replace(  # noqa: E501
        "production_", ""
    ),
    "NEXT_PUBLIC_LEARN_AI_RECOMMENDATION_ENDPOINT": nextjs_config.require(
        "recommendation_endpoint"
    ),
    "NEXT_PUBLIC_LEARN_AI_SYLLABUS_ENDPOINT": nextjs_config.require(
        "syllabus_endpoint"
    ),
    "NEXT_PUBLIC_MITOL_API_BASE_URL": nextjs_config.require("mitlearn_api_base_url"),
    "NEXT_PUBLIC_MITX_ONLINE_CSRF_COOKIE_NAME": "csrf_mitxonline",
    "NEXT_PUBLIC_MITX_ONLINE_BASE_URL": nextjs_config.require("mitxonline_base_url"),
    "NEXT_PUBLIC_MITX_ONLINE_LEGACY_BASE_URL": nextjs_config.require(
        "mitxonline_legacy_base_url"
    ),
    "NEXT_PUBLIC_MITOL_AXIOS_WITH_CREDENTIALS": "true",
    "NEXT_PUBLIC_MITOL_SUPPORT_EMAIL": "mitlearn-support@mit.edu",
    "NEXT_PUBLIC_ORIGIN": nextjs_config.require("origin"),
    "NEXT_PUBLIC_POSTHOG_API_HOST": nextjs_config.require("posthog_api_host"),
    "NEXT_PUBLIC_PODCASTS_FEATURED_LIST_LEARNINGPATH_ID": (
        nextjs_config.get("podcasts_featured_list_learningpath_id") or ""
    ),
    "NEXT_PUBLIC_POSTHOG_API_KEY": nextjs_config.require("posthog_api_key"),
    "NEXT_PUBLIC_POSTHOG_PROJECT_ID": nextjs_config.require("posthog_project_id"),
    "NEXT_PUBLIC_POSTHOG_UI_HOST": "https://us.posthog.com",
    "NEXT_PUBLIC_SENTRY_DSN": nextjs_config.require("sentry_dsn"),
    "NEXT_PUBLIC_SENTRY_ENV": nextjs_config.require("sentry_env"),
    "NEXT_PUBLIC_SENTRY_PROFILES_SAMPLE_RATE": "0.25",
    "NEXT_PUBLIC_SENTRY_TRACES_SAMPLE_RATE": "0.001",
    "NEXT_PUBLIC_SITE_NAME": "MIT Learn",
    "NEXT_PUBLIC_STAY_UPDATED_HUBSPOT_FORM_ID": stay_updated_hubspot_form_id,
    "NEXT_PUBLIC_VERSION": MIT_LEARN_NEXTJS_DOCKER_TAG,
    "NEXT_PUBLIC_FEATURE_product_page_courses": "false",
    "NEXT_PUBLIC_FEATURE_article_viewer": "true",
    "NEXT_PUBLIC_FEATURE_video_shorts": "true",
    "NEXT_PUBLIC_FEATURE_enrollment_dashboard": "false",  # pragma: allowlist secret
    "NEXT_PUBLIC_FEATURE_lr_drawer_chatbot": "true",
    "NEXT_PUBLIC_FEATURE_home_page_recommendation_bot": "true",  # pragma: allowlist secret  # noqa: E501
    # OpenTelemetry — server-side only (no NEXT_PUBLIC_ prefix).
    # OTEL_EXPORTER_OTLP_ENDPOINT is the base URL; the Node.js SDK appends /v1/traces.
    # OTEL_TRACES_SAMPLER_ARG is read by sentry.server.config.ts as tracesSampleRate.
    # OTEL_RESOURCE_ATTRIBUTES is read by Sentry's OTEL provider for span metadata.
    #
    # The following three vars are set here for consistency with other applications
    # but are NOT read by the Next.js Sentry-managed OTEL provider:
    #   OTEL_TRACES_SAMPLER  — Sentry uses SentrySampler (not the standard OTEL
    #                          env var), which already implements parent-based sampling
    #                          by inheriting a sampled traceparent before applying the
    #                          tracesSampleRate fallback.
    #   OTEL_PROPAGATORS     — Sentry sets its own composite propagator (W3C
    #                          tracecontext + baggage + Sentry) internally.
    #   OTEL_EXPORTER_OTLP_PROTOCOL — the OTLPTraceExporter is instantiated directly
    #                          in sentry.server.config.ts; protocol negotiation is
    #                          handled by the exporter's own defaults.
    "OTEL_SERVICE_NAME": "learn-nextjs",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://grafana-k8s-monitoring-alloy-receiver.grafana.svc.cluster.local:4318",
    "OTEL_TRACES_SAMPLER": "parentbased_traceidratio",
    "OTEL_TRACES_SAMPLER_ARG": "0.25",
    "OTEL_PROPAGATORS": "tracecontext,baggage",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
    "OTEL_RESOURCE_ATTRIBUTES": (
        f"deployment.environment={stack_info.env_suffix}"
        f",service.namespace=learn"
        f",service.version={MIT_LEARN_NEXTJS_DOCKER_TAG}"
    ),
}

merge_otel_resource_attributes(raw_env_vars, k8s_app_labels)

env_vars = []
for k, v in raw_env_vars.items():
    env_vars.append(
        kubernetes.core.v1.EnvVarArgs(
            name=k,
            value=v,
        )
    )

env_vars.append(
    kubernetes.core.v1.EnvVarArgs(
        name="NEXT_PUBLIC_RECAPTCHA_SITE_KEY",
        value_from=kubernetes.core.v1.EnvVarSourceArgs(
            secret_key_ref=kubernetes.core.v1.SecretKeySelectorArgs(
                name="mitopen-static-secret",
                key="RECAPTCHA_SITE_KEY",
                optional=True,
            ),
        ),
    )
)


pod_count = nextjs_config.get_int("pod_count") or 2

mit_learn_nextjs_deployment = kubernetes.apps.v1.Deployment(
    f"mit-learn-nextjs-{stack_info.name}-deployment",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mit-learn-nextjs",
        namespace=learn_namespace,
        labels=k8s_app_labels,
        annotations={
            "deployment.kubernetes.io/description": (
                "MIT Learn Next.js application (standalone build)"
            )
        },
    ),
    spec=kubernetes.apps.v1.DeploymentSpecArgs(
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=k8s_app_labels,
        ),
        replicas=pod_count,
        min_ready_seconds=10,
        strategy=kubernetes.apps.v1.DeploymentStrategyArgs(
            type="RollingUpdate",
            rolling_update=kubernetes.apps.v1.RollingUpdateDeploymentArgs(
                max_unavailable=0,
                max_surge=1,
            ),
        ),
        template=kubernetes.core.v1.PodTemplateSpecArgs(
            metadata=kubernetes.meta.v1.ObjectMetaArgs(
                labels=k8s_app_labels,
            ),
            spec=kubernetes.core.v1.PodSpecArgs(
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
                            requests={"cpu": "100m", "memory": nextjs_memory_limit},
                            limits={"memory": nextjs_memory_limit},
                        ),
                        env=env_vars,
                        liveness_probe=kubernetes.core.v1.ProbeArgs(
                            tcp_socket=kubernetes.core.v1.TCPSocketActionArgs(
                                port=DEFAULT_NEXTJS_PORT,
                            ),
                            initial_delay_seconds=30,
                            period_seconds=30,
                            failure_threshold=3,
                        ),
                        readiness_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/healthcheck",
                                port=DEFAULT_NEXTJS_PORT,
                            ),
                            initial_delay_seconds=15,
                            period_seconds=15,
                            failure_threshold=3,
                        ),
                        startup_probe=kubernetes.core.v1.ProbeArgs(
                            http_get=kubernetes.core.v1.HTTPGetActionArgs(
                                path="/healthcheck",
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
)

kubernetes.policy.v1.PodDisruptionBudget(
    f"mit-learn-nextjs-{stack_info.name}-pdb",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mit-learn-nextjs-pdb",
        namespace=learn_namespace,
        labels=k8s_app_labels,
    ),
    spec=kubernetes.policy.v1.PodDisruptionBudgetSpecArgs(
        max_unavailable=1,
        selector=kubernetes.meta.v1.LabelSelectorArgs(
            match_labels=k8s_app_labels,
        ),
    ),
)

mit_learn_nextjs_service_name = "mit-learn-nextjs"

mit_learn_nextjs_service = kubernetes.core.v1.Service(
    f"mit-learn-nextjs-{stack_info.name}-service-resource",
    metadata=kubernetes.meta.v1.ObjectMetaArgs(
        name="mit-learn-nextjs",
        namespace=learn_namespace,
        labels=k8s_app_labels,
        annotations={"pulumi.com/patchForce": "true"},
    ),
    spec=kubernetes.core.v1.ServiceSpecArgs(
        selector=k8s_app_labels,
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
        depends_on=[mit_learn_nextjs_deployment],
    ),
)

gateway = OLEKSGateway(
    f"mit-learn-nextjs-{stack_info.name}-gateway",
    gateway_config=OLEKSGatewayConfig(
        cert_issuer="letsencrypt-production",
        cert_issuer_class="cluster-issuer",
        gateway_name="mit-learn-nextjs-gateway",
        labels=k8s_app_labels,
        namespace=learn_namespace,
        rate_limit=OLEKSGatewayRateLimitConfig(
            average=nextjs_config.get_int("rate_limit_average") or 300,
            burst=nextjs_config.get_int("rate_limit_burst") or 600,
        ),
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
                filters=[
                    {
                        "type": "ResponseHeaderModifier",
                        "responseHeaderModifier": {
                            "add": [
                                {
                                    "name": "X-Robots-Tag",
                                    "value": "noindex, nofollow",
                                }
                            ]
                        },
                    }
                ],
            ),
        ],
    ),
    opts=ResourceOptions(
        delete_before_replace=True,
    ),
)

export("domain", nextjs_config.require("domain"))
export("image", app_image)
