# Shared Tilt helpers for local-dev.
#
# Load in any Tiltfile with:
#   load("../../tiltlib.star", "k8s_yaml_local")  # from apps/<app>/
#   load("./local-dev/tiltlib.star", "k8s_yaml_local")  # from repo root
#
# k8s_yaml_local applies manifests with two local-dev conveniences:
#
# 1. Root-domain substitution. The LOCAL_DEV_ROOT_DOMAIN environment variable
#    (default: mit.dev) replaces every 'mit.dev' occurrence so hostnames,
#    URLs, and cookie-domain references update consistently:
#      export LOCAL_DEV_ROOT_DOMAIN=mycompany.dev && tilt up
#
# 2. Gitignored per-developer overrides. Pass local_overrides= the path of an
#    optional, gitignored ConfigMap manifest (conventionally
#    configmaps/app-env.local.yaml, named <app>-env-local). When the file
#    exists it is applied with the other manifests; each app's
#    deployment.yaml references it as the LAST envFrom entry with
#    optional: true, so its keys override both the tracked ConfigMap and the
#    tracked Secret, and its absence is a no-op. Because Kubernetes does not
#    restart pods when a ConfigMap changes, every Deployment applied in the
#    same call gets a pod-template annotation fingerprinting the overrides —
#    editing the override file rolls the pods so new values take effect.
#    See "Local Configuration Overrides" in local-dev/README.md.

_ROOT_DOMAIN_DEFAULT = "mit.dev"
_OVERRIDE_HASH_ANNOTATION = "ol.mit.edu/local-overrides-hash"

def _overrides_fingerprint(path, text):
    """Validate the override manifest and return a stable fingerprint of its
    data (not the raw text, so comment/formatting edits don't roll pods).
    Also logs the overridden key names — never values — to the Tiltfile log."""
    docs = [d for d in decode_yaml_stream(text) if d != None]
    if len(docs) != 1 or docs[0].get("kind") != "ConfigMap":
        fail("%s: expected a single ConfigMap manifest" % path)
    # A data: key with nothing (or only comments) under it parses as None.
    data = docs[0].get("data", {}) or {}
    for k in data:
        if type(data[k]) != "string":
            fail(
                '%s: data.%s must be a YAML string — quote the value (e.g. "True", "8080"); got %s'
                % (path, k, type(data[k]))
            )
    keys = sorted(data.keys())
    print("[%s] local overrides active: %s" % (path, ", ".join(keys) if keys else "(none)"))
    if not keys:
        # Empty data: treat as "no overrides" — apply the (empty) ConfigMap
        # but skip stamping, so Deployments return to their unannotated state.
        return ""
    canon = "".join(["%s=%s\n" % (k, data[k]) for k in keys])
    return str(hash(canon))

def _stamp_deployments(content, fingerprint):
    """Add the overrides-fingerprint annotation to every Deployment pod
    template in a (possibly multi-doc) manifest text, so changing an override
    rolls the pods. Returns a Blob, or None if there are no Deployments."""
    docs = [d for d in decode_yaml_stream(content) if d != None]
    stamped = False
    for d in docs:
        if d.get("kind") == "Deployment":
            annotations = (
                d.setdefault("spec", {})
                .setdefault("template", {})
                .setdefault("metadata", {})
                .setdefault("annotations", {})
            )
            annotations[_OVERRIDE_HASH_ANNOTATION] = fingerprint
            stamped = True
    if not stamped:
        return None
    return encode_yaml_stream(docs)

def k8s_yaml_local(paths, local_overrides=None):
    """Apply k8s YAML with root-domain substitution and an optional
    gitignored ConfigMap of per-developer overrides (see module docstring)."""
    rd = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", _ROOT_DOMAIN_DEFAULT)

    # read_file(default=...) also registers a watch on the override path —
    # including its creation — so adding or editing it mid-session re-runs
    # the Tiltfile.
    overrides_text = ""
    if local_overrides:
        overrides_text = str(read_file(local_overrides, default=""))

    if rd == _ROOT_DOMAIN_DEFAULT and not overrides_text.strip():
        # Fast path: nothing to rewrite, apply files as-is.
        k8s_yaml(paths)
        return

    fingerprint = ""
    all_paths = list(paths)
    if overrides_text.strip():
        fingerprint = _overrides_fingerprint(local_overrides, overrides_text)
        all_paths.append(local_overrides)

    for p in all_paths:
        content = str(read_file(p))
        if rd != _ROOT_DOMAIN_DEFAULT:
            content = content.replace(_ROOT_DOMAIN_DEFAULT, rd)
        stamped = _stamp_deployments(content, fingerprint) if fingerprint else None
        k8s_yaml(stamped if stamped != None else blob(content))
