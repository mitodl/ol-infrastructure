# Shared Tilt helpers for local-dev.
#
# Load in any Tiltfile with:
#   load("../../tiltlib.star", "k8s_yaml_with_domain")  # from apps/<app>/
#   load("./local-dev/tiltlib.star", "k8s_yaml_with_domain")  # from repo root
#
# Root domain is read from the LOCAL_DEV_ROOT_DOMAIN environment variable
# (default: mit.dev). Set it before running tilt up to use a custom domain:
#   export LOCAL_DEV_ROOT_DOMAIN=mycompany.dev && tilt up

def k8s_yaml_with_domain(paths):
    """Apply k8s YAML, substituting LOCAL_DEV_ROOT_DOMAIN for 'mit.dev'.

    When the env var is unset or equals the default 'mit.dev', files are
    applied as-is with no overhead.  When overridden, each file is processed
    through sed before being applied so that all hostnames, URLs, and
    cookie-domain references update consistently.
    """
    rd = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", "mit.dev")
    if rd == "mit.dev":
        k8s_yaml(paths)
        return
    for p in paths:
        content = str(local(
            "sed 's/mit\\.dev/{rd}/g' {p}".format(rd = rd, p = p),
            quiet = True,
        ))
        k8s_yaml(blob(content))
