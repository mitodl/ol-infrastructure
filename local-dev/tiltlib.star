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
#    tracked files. The overlay also lists <app_dir>/local/app-env.local.yaml,
#    the gitignored env-override ConfigMap that every container's envFrom
#    references last; nothing here knows about that file beyond warning when
#    it exists without the overlay. Tilt shells out to `kustomize`, or to
#    `kubectl kustomize` when the standalone binary is not installed. Tilt's
#    own parser decides which of the kustomization's inputs are watched:
#    resources (including nested kustomizations), patch files, crds, and
#    configMapGenerator `files` re-run the Tiltfile on edit; configMapGenerator
#    `envs`, secretGenerator sources, and replacement files do not.
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
#    app-env.yaml/secrets.yaml, or the overlay's app-env.local.yaml) rolls the
#    pods so new values actually take effect.

_ROOT_DOMAIN_DEFAULT = "mit.dev"
_CONFIG_HASH_ANNOTATION = "ol.mit.edu/config-hash"

def _config_fingerprint(path, text):
    """Return a stable fingerprint of the combined data/binaryData/stringData
    of every ConfigMap and Secret in a (possibly multi-doc) manifest text (not
    the raw text, so comment/formatting-only edits don't roll pods). `path`
    only labels error messages."""
    pairs = []
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
    with root-domain substitution and the config-fingerprint pod-template
    annotation (see module docstring)."""
    rd = os.environ.get("LOCAL_DEV_ROOT_DOMAIN", _ROOT_DOMAIN_DEFAULT)

    base_dir = os.path.join(app_dir, "base")
    overlay_dir = os.path.join(app_dir, "local")

    # DELETE ANYTIME AFTER 2026-11-14, together with
    # local-dev/scripts/migrate-local-overrides.sh. The env-override ConfigMap
    # used to live in <app_dir>/configmaps/; nothing reads it there, so without
    # this a developer's overrides would vanish silently on checkout.
    legacy_env_overrides = os.path.join(app_dir, "configmaps", "app-env.local.yaml")
    if str(read_file(legacy_env_overrides, default="")).strip():
        fail(
            "%s is no longer applied; it belongs in %s/. Run "
            % (legacy_env_overrides, overlay_dir)
            + "local-dev/scripts/start.sh (or local-dev/scripts/migrate-local-overrides.sh) "
            + "to move it and create the overlay beside it."
        )
    overlay_kustomization = os.path.join(overlay_dir, "kustomization.yaml")
    env_overrides = os.path.join(overlay_dir, "app-env.local.yaml")

    # read_file(default=...) registers a watch on a path that does not exist
    # yet, so creating either gitignored file mid-session re-runs the
    # Tiltfile. os.path.exists() would not.
    if str(read_file(overlay_kustomization, default="")).strip():
        print("[%s] local kustomize overlay active" % overlay_kustomization)
        manifests_dir = overlay_dir
    else:
        # The env-override ConfigMap only reaches the cluster as a resource of
        # the overlay, so on its own the file would silently do nothing.
        if str(read_file(env_overrides, default="")).strip():
            fail(
                "%s exists but %s does not, so it is not applied. Copy the "
                % (env_overrides, overlay_kustomization)
                + "kustomization.yaml.example beside it into place (it lists "
                + 'app-env.local.yaml as a resource); see "Local Configuration '
                + 'Overrides" in local-dev/README.md.'
            )
        manifests_dir = base_dir
    content = str(kustomize(manifests_dir))

    if rd != _ROOT_DOMAIN_DEFAULT:
        content = content.replace(_ROOT_DOMAIN_DEFAULT, rd)

    stamped = _stamp_deployments(content, _config_fingerprint(manifests_dir, content))
    k8s_yaml(stamped if stamped != None else blob(content))
