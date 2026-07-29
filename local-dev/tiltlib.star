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
# 2. Config-change rollouts. Kubernetes does not restart pods when a
#    ConfigMap/Secret they reference changes, so every Deployment applied in
#    the same call gets a pod-template annotation fingerprinting the combined
#    data/stringData of every ConfigMap and Secret passed in — editing any of
#    them (the tracked app-env.yaml/secrets.yaml, or an optional gitignored
#    local_overrides ConfigMap passed for per-developer overrides — see
#    "Local Configuration Overrides" in local-dev/README.md) rolls the pods
#    so new values actually take effect.

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

def k8s_yaml_local(paths, local_overrides=None):
    """Apply k8s YAML with root-domain substitution and a pod-template
    fingerprint annotation covering every applied ConfigMap/Secret, including
    an optional gitignored ConfigMap of per-developer overrides (see module
    docstring)."""
    rd = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", _ROOT_DOMAIN_DEFAULT)

    # read_file(default=...) also registers a watch on the override path —
    # including its creation — so adding or editing it mid-session re-runs
    # the Tiltfile.
    overrides_text = ""
    if local_overrides:
        overrides_text = str(read_file(local_overrides, default=""))
        if overrides_text.strip():
            docs = [d for d in decode_yaml_stream(overrides_text) if d != None]
            if len(docs) != 1 or docs[0].get("kind") != "ConfigMap":
                fail("%s: expected a single ConfigMap manifest" % local_overrides)
            keys = sorted((docs[0].get("data", {}) or {}).keys())
            print("[%s] local overrides active: %s" % (local_overrides, ", ".join(keys) if keys else "(none)"))

    all_paths = list(paths)
    if overrides_text.strip():
        all_paths.append(local_overrides)

    contents = []
    for p in all_paths:
        content = str(read_file(p))
        if rd != _ROOT_DOMAIN_DEFAULT:
            content = content.replace(_ROOT_DOMAIN_DEFAULT, rd)
        contents.append((p, content))

    fingerprint = _config_fingerprint(contents)

    for p, content in contents:
        stamped = _stamp_deployments(content, fingerprint)
        k8s_yaml(stamped if stamped != None else blob(content))
