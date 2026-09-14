# Shared Tilt helpers for local-dev.
#
# Load in any app Tiltfile with:
#   load("../../tiltlib.star", "k8s_yaml_app")
#
# k8s_yaml_app(app_dir) applies an app's manifests with three local-dev
# conveniences (all described under "Local Configuration Overrides" in
# local-dev/README.md):
#
# 1. kustomize build with an optional per-developer overlay. The tracked
#    manifests live in <app_dir>/base/ with a kustomization.yaml listing them.
#    When the gitignored <app_dir>/local/kustomization.yaml exists, that
#    overlay is built instead of base/, so any field of any tracked manifest
#    (a memory limit, a probe, an image tag) can be changed without editing
#    tracked files. Tilt shells out to `kustomize`, or to `kubectl kustomize`
#    when the standalone binary is not installed.
#
# 2. Root-domain substitution. The LOCAL_DEV_ROOT_DOMAIN environment variable
#    (default: mit.dev) replaces every 'mit.dev' occurrence so hostnames,
#    URLs, and cookie-domain references update consistently:
#      export LOCAL_DEV_ROOT_DOMAIN=mycompany.dev && tilt up
#
# 3. Config-change rollouts. Kubernetes does not restart pods when a
#    ConfigMap/Secret they reference changes, so every applied Deployment gets
#    a pod-template annotation fingerprinting the combined data/stringData of
#    every applied ConfigMap and Secret — editing any of them (the tracked
#    app-env.yaml/secrets.yaml, or the optional gitignored
#    <app_dir>/local/app-env.local.yaml override ConfigMap) rolls the pods so
#    new values actually take effect.

_ROOT_DOMAIN_DEFAULT = "mit.dev"
_CONFIG_HASH_ANNOTATION = "ol.mit.edu/config-hash"

def _config_fingerprint(paths_and_texts):
    """Return a stable fingerprint of the combined data/binaryData/stringData
    of every ConfigMap and Secret across the given (path, text) pairs (not the
    raw text, so comment/formatting-only edits don't roll pods)."""
    pairs = []
    for path, text in paths_and_texts:
        docs = [d for d in decode_yaml_stream(text) if d != None]
        for d in docs:
            kind = d.get("kind")
            if kind != "ConfigMap" and kind != "Secret":
                continue
            # Kubernetes merges all three value fields rather than picking one,
            # with stringData taking precedence over data on key collisions, so
            # fingerprint the union — otherwise an edit to a field we skipped
            # would silently not roll the pods.
            data = dict(d.get("data") or {})
            data.update(d.get("binaryData") or {})
            data.update(d.get("stringData") or {})
            name = d.get("metadata", {}).get("name", "?")
            for k in sorted(data.keys()):
                v = data[k]
                # A `KEY:` with nothing after it parses as YAML null; Kubernetes'
                # own JSON decode leaves map[string]string entries untouched (i.e.
                # "") on null, so mirror that instead of failing on it.
                if v == None:
                    v = ""
                elif type(v) != "string":
                    fail(
                        '%s: %s %s: key %s must be a YAML string — quote the value (e.g. "True", "8080"); got %s'
                        % (path, kind, name, k, type(v))
                    )
                pairs.append("%s/%s=%s" % (name, k, v))
    return str(hash("\n".join(sorted(pairs))))

def _stamp_deployments(content, fingerprint):
    """Add the config-fingerprint annotation to every Deployment pod
    template in a (possibly multi-doc) manifest text, so changing any
    applied ConfigMap/Secret rolls the pods. Returns a Blob, or None if
    there are no Deployments."""
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
            annotations[_CONFIG_HASH_ANNOTATION] = fingerprint
            stamped = True
    if not stamped:
        return None
    return encode_yaml_stream(docs)

def k8s_yaml_app(app_dir):
    """Apply an app's manifests: a kustomize build of <app_dir>/local/ when
    the developer has created its kustomization.yaml, else of <app_dir>/base/;
    plus the optional gitignored <app_dir>/local/app-env.local.yaml override
    ConfigMap; with root-domain substitution and the config-fingerprint
    pod-template annotation (see module docstring)."""
    rd = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", _ROOT_DOMAIN_DEFAULT)

    base_dir = os.path.join(app_dir, "base")
    overlay_dir = os.path.join(app_dir, "local")
    overlay_kustomization = os.path.join(overlay_dir, "kustomization.yaml")
    local_overrides = os.path.join(overlay_dir, "app-env.local.yaml")

    # read_file(default=...) registers a watch on a path that does not exist
    # yet, so creating either gitignored file mid-session re-runs the
    # Tiltfile. os.path.exists() would not.
    if str(read_file(overlay_kustomization, default="")).strip():
        print("[%s] local kustomize overlay active" % overlay_kustomization)
        manifests_dir = overlay_dir
    else:
        manifests_dir = base_dir
    contents = [(manifests_dir, str(kustomize(manifests_dir)))]

    overrides_text = str(read_file(local_overrides, default=""))
    if overrides_text.strip():
        docs = [d for d in decode_yaml_stream(overrides_text) if d != None]
        if len(docs) != 1 or docs[0].get("kind") != "ConfigMap":
            fail("%s: expected a single ConfigMap manifest" % local_overrides)
        keys = sorted((docs[0].get("data", {}) or {}).keys())
        print("[%s] local overrides active: %s" % (local_overrides, ", ".join(keys) if keys else "(none)"))
        contents.append((local_overrides, overrides_text))

    if rd != _ROOT_DOMAIN_DEFAULT:
        contents = [(p, content.replace(_ROOT_DOMAIN_DEFAULT, rd)) for p, content in contents]

    fingerprint = _config_fingerprint(contents)

    for p, content in contents:
        stamped = _stamp_deployments(content, fingerprint)
        k8s_yaml(stamped if stamped != None else blob(content))
