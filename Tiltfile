# MIT Learn Stack — Local Development Entry Point
# Run: tilt up
# Docs: local-dev/README.md

# ---------------------------------------------------------------------------
# Developer configuration
# ---------------------------------------------------------------------------
config.define_string_list("enabled_apps", usage="Apps to run: mit-learn learn-ai mitxonline odl-video-service")
config.define_bool("per_app_databases", usage="Deploy isolated DB/Valkey per app namespace")
config.define_string("openedx_mode", usage="qa (default) or local (Tutor)")
config.define_string_list("prebuilt_tags", usage="Prebuilt image tag overrides per app, e.g. mit-learn=0.62.0 learn-ai=0.28.3")
config.define_string("openai_api_key", usage="OpenAI API key for AI/embedding features (optional)")
config.define_string("langsmith_api_key", usage="LangSmith API key for tracing (optional)")
config.define_string("canvas_ai_token", usage="Canvas AI token (optional)")
config.define_string("root_domain", usage="Root DNS domain for all local-dev services (default: mit.dev). Overrides LOCAL_DEV_ROOT_DOMAIN env var.")
cfg = config.parse()

enabled_apps = cfg.get("enabled_apps", ["mit-learn", "learn-ai", "mitxonline", "odl-video-service"])
per_app_databases = cfg.get("per_app_databases", False)
openedx_mode = cfg.get("openedx_mode", "qa")

# Root domain: configurable via tilt config or LOCAL_DEV_ROOT_DOMAIN env var.
# All service hostnames are derived from this value — changing it rewires
# every URL, CORS origin, APISIX route, and Keycloak redirect URI at once.
# NOTE: root_domain is passed to Pulumi and k8s manifests via LOCAL_DEV_ROOT_DOMAIN;
# tilt config root_domain takes precedence over the env var.
root_domain = cfg.get("root_domain") or os.environ.get("LOCAL_DEV_ROOT_DOMAIN", "mit.dev")

# Parse prebuilt_tags list (["app=tag", ...]) into a lookup dict.
prebuilt_tags = {
    kv.split("=")[0]: kv.split("=")[1]
    for kv in cfg.get("prebuilt_tags", [])
    if "=" in kv
}

# Workspace root: directory that contains ol-infrastructure and sibling app repos.
# Override with MITOL_WORKSPACE_ROOT environment variable.
workspace_root = os.environ.get("MITOL_WORKSPACE_ROOT", config.main_dir + "/..")

# ---------------------------------------------------------------------------
# Application registry
#
# seed_commands: list of shell commands run via `kubectl exec` into the web
# pod after the deployment is healthy.  Each entry is a dict with:
#   - label: short name shown in Tilt UI
#   - cmd:   shell command to exec inside the pod
# All seed resources are TRIGGER_MODE_MANUAL — they never run automatically.
# ---------------------------------------------------------------------------
APPS = [
    {
        "name": "mit-learn",
        "dir": "mit-learn",
        "namespace": "mit-learn",
        "deploy_name": "mitlearn-webapp",
        "image_backend": "mitodl/mit-learn-app",
        "image_frontend": "mitodl/mit-learn-nextjs-app",
        "prebuilt_tag_backend": prebuilt_tags.get("mit-learn", "0.62.0"),
        "prebuilt_tag_frontend": prebuilt_tags.get("mit-learn-nextjs", "0.62.0"),
        "tiltfile": "./local-dev/apps/mit-learn/Tiltfile",
        "tiltfile_frontend": "./local-dev/apps/mit-learn-nextjs/Tiltfile",
        # Seeding commands; executed inside the web pod via `kubectl exec`.
        # bootstrap runs automatically on first deploy (see apps/mit-learn/Tiltfile);
        # the entries here are optional / on-demand enrichment tasks.
        "seed_commands": [
            {
                "label": "seed-mit-learn-fixtures",
                "description": "Load core fixtures: platforms, schools, departments, offered_by",
                "cmd": "python manage.py loaddata platforms schools departments offered_by",
            },
            {
                "label": "seed-mit-learn-qdrant",
                "description": "Create Qdrant vector-search collections",
                "cmd": "python manage.py create_qdrant_collections",
            },
            {
                "label": "seed-mit-learn-opensearch",
                "description": "Recreate OpenSearch index from scratch",
                "cmd": "python manage.py recreate_index",
            },
            {
                "label": "seed-mit-learn-ocw",
                "description": "Backpopulate OCW learning resources (network access required)",
                "cmd": "python manage.py backpopulate_ocw_data",
            },
            {
                "label": "seed-mit-learn-mitxonline",
                "description": "Backpopulate MITx Online resources",
                "cmd": "python manage.py backpopulate_mitxonline_data",
            },
        ],
    },
    {
        "name": "learn-ai",
        "dir": "learn-ai",
        "namespace": "learn-ai",
        "deploy_name": "learnai-webapp",
        "image_backend": "mitodl/learn-ai-app",
        "prebuilt_tag_backend": prebuilt_tags.get("learn-ai", "0.28.3"),
        "tiltfile": "./local-dev/apps/learn-ai/Tiltfile",
        "seed_commands": [
            {
                "label": "seed-learn-ai-checkpoints",
                "description": "Backpopulate tutor checkpoints from Open edX",
                "cmd": "python manage.py backpopulate_tutor_checkpoints",
            },
        ],
    },
    {
        "name": "mitxonline",
        "dir": "mitxonline",
        "namespace": "mitxonline",
        "deploy_name": "mitxonline-webapp",
        "image_backend": "mitodl/mitxonline-app",
        "prebuilt_tag_backend": prebuilt_tags.get("mitxonline", "1.144.5"),
        "tiltfile": "./local-dev/apps/mitxonline/Tiltfile",
        "seed_commands": [
            {
                "label": "seed-mitxonline-instance",
                "description": "Full instance setup: superuser, OAuth2 app, program, courses, products",
                "cmd": "python manage.py configure_instance",
            },
            {
                "label": "seed-mitxonline-course-data",
                "description": "Populate test course data from courses.json",
                "cmd": "python manage.py populate_course_data",
            },
            {
                "label": "seed-mitxonline-income-thresholds",
                "description": "Load country income thresholds for financial assistance",
                "cmd": "python manage.py load_country_income_thresholds flexiblepricing/fixtures/country_income_threshold_data.json",
            },
        ],
    },
    {
        "name": "odl-video-service",
        "dir": "odl-video-service",
        "namespace": "odl-video-service",
        "deploy_name": "odlvideo-webapp",
        "image_backend": "mitodl/odl-video-service-app",
        "prebuilt_tag_backend": prebuilt_tags.get("odl-video-service", "0.85.0"),
        "tiltfile": "./local-dev/apps/odl-video-service/Tiltfile",
        "seed_commands": [
            {
                "label": "seed-ovs-presets",
                "description": "Create video encoding presets",
                "cmd": "python manage.py createpresets",
            },
        ],
    },
]

# ---------------------------------------------------------------------------
# Shared infrastructure (Pulumi stacks)
# ---------------------------------------------------------------------------

# Core stack (operators, foundational services, Keycloak instance)
local_resource(
    "local-infra-core",
    cmd="LOCAL_DEV_ROOT_DOMAIN={rd} PULUMI_CONFIG_PASSPHRASE='' bash -c 'pulumi stack init local-dev.core.Dev 2>/dev/null; pulumi refresh --yes --skip-preview --stack local-dev.core.Dev && pulumi up --yes --skip-preview --logtostderr --stack local-dev.core.Dev'".format(rd=root_domain),
    dir="./local-dev/infra/core",
    deps=["./local-dev/infra/modules", "./local-dev/infra/core/__main__.py"],
    labels=["infra"],
)

# Apps infrastructure stack (databases, Keycloak realm) — depends on core.
#
# The Keycloak provider talks to the realm over the public ingress, so we wait
# for Keycloak's admin REST API to be ready before running `pulumi up`.
# Gating on the OIDC discovery endpoint alone is insufficient: discovery comes
# up well before the admin API can service writes, so the provider's
# POST /admin/realms races into the warm-up window and times out
# ("context deadline exceeded"). The script below proves the admin API is
# serving (admin token + authenticated GET /admin/realms) before we proceed.
#
# `pulumi refresh` is intentionally omitted from this loop: it issues a burst
# of concurrent admin-API calls on every reconcile, which was the main source
# of the warm-up 502s. Refresh on demand instead.
local_resource(
    "local-infra-apps",
    cmd="LOCAL_DEV_ROOT_DOMAIN={rd} PULUMI_CONFIG_PASSPHRASE='' bash -c '{wait} sso.ol.{rd} && {{ pulumi stack init local-dev.apps-infra.Dev 2>/dev/null; pulumi up --yes --skip-preview --logtostderr --stack local-dev.apps-infra.Dev; }}'".format(rd=root_domain, wait="{}/local-dev/scripts/wait-for-keycloak-admin.sh".format(config.main_dir)),
    dir="./local-dev/infra/apps_infra",
    deps=["./local-dev/infra/modules", "./local-dev/infra/apps_infra/__main__.py", "./local-dev/scripts/wait-for-keycloak-admin.sh"],
    labels=["infra"],
    resource_deps=["local-infra-core"],
)

# ---------------------------------------------------------------------------
# Per-app deployment + manual seed resources
# ---------------------------------------------------------------------------
for app in [a for a in APPS if a["name"] in enabled_apps]:
    if os.path.exists(app["tiltfile"]):
        include(app["tiltfile"])

    # Include frontend Tiltfile if defined and present (e.g., mit-learn Next.js)
    frontend_tiltfile = app.get("tiltfile_frontend", "")
    if frontend_tiltfile and os.path.exists(frontend_tiltfile):
        include(frontend_tiltfile)
    # Register one manual-trigger Tilt resource per seed command.
    # These are never auto-run; trigger them from the Tilt UI or with:
    #   tilt trigger seed-<app>-<label>
    # or via:
    #   ./local-dev/scripts/seed.sh --app <app> --cmd <label>
    for seed in app.get("seed_commands", []):
        exec_cmd = (
            "kubectl exec -n {ns} deploy/{deploy} -- {cmd}".format(
                ns=app["namespace"],
                deploy=app["deploy_name"],
                cmd=seed["cmd"],
            )
        )
        local_resource(
            seed["label"],
            cmd=exec_cmd,
            resource_deps=[app["deploy_name"]],
            labels=["seed"],
            trigger_mode=TRIGGER_MODE_MANUAL,
            auto_init=False,
        )
