# Shared Tilt helpers for local-dev.
#
# Load in any Tiltfile with:
#   load("../../tiltlib.star", "k8s_yaml_local")  # from apps/<app>/
#   load("./local-dev/tiltlib.star", "k8s_yaml_local")  # from repo root
#
# k8s_yaml_local applies manifests with three local-dev conveniences:
#
# 1. Root-domain substitution. The LOCAL_DEV_ROOT_DOMAIN environment variable
#    (default: mit.test) replaces every 'mit.test' occurrence so hostnames,
#    URLs, and cookie-domain references update consistently:
#      export LOCAL_DEV_ROOT_DOMAIN=mycompany.test && tilt up
#
# 2. Config-change rollouts. Kubernetes does not restart pods when a
#    ConfigMap/Secret they reference changes, so every Deployment and
#    StatefulSet applied in the same call gets a pod-template annotation
#    fingerprinting the combined
#    data/stringData of every ConfigMap and Secret passed in — editing any of
#    them (the tracked app-env.yaml/secrets.yaml, or an optional gitignored
#    local_overrides ConfigMap passed for per-developer overrides — see
#    "Local Configuration Overrides" in local-dev/README.md) rolls the pods
#    so new values actually take effect.
#
# 3. Published image tags. The root Tiltfile sets LOCAL_DEV_PREBUILT_IMAGE_TAGS
#    to a JSON object of image name -> tag for the apps that have a
#    prebuilt_tags entry and no checkout. Deployment containers running one
#    of those images get that tag in place of the one in the manifest.

_ROOT_DOMAIN_DEFAULT = "mit.test"
_CONFIG_HASH_ANNOTATION = "ol.mit.edu/config-hash"
_PREBUILT_IMAGE_TAGS_ENV = "LOCAL_DEV_PREBUILT_IMAGE_TAGS"


def current_root_domain():
    """Return the value k8s_yaml_local substitutes 'mit.test' for, so a caller
    that needs the same value for something other than YAML (e.g. folding it
    into a downstream checksum input) doesn't duplicate the env var name or
    default."""
    return os.environ.get("LOCAL_DEV_ROOT_DOMAIN", _ROOT_DOMAIN_DEFAULT)


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

def _image_name(image):
    """Return an image reference without its tag or digest."""
    name = image.split("@")[0]
    if ":" in name.split("/")[-1]:
        name = name.rsplit(":", 1)[0]
    return name

def _pin_images(pod_spec, image_tags):
    """Point every container whose image is in image_tags at that tag."""
    containers = (pod_spec.get("initContainers") or []) + (pod_spec.get("containers") or [])
    for c in containers:
        name = _image_name(c.get("image", ""))
        if name in image_tags:
            c["image"] = "%s:%s" % (name, image_tags[name])

def _stamp_workloads(content, fingerprint, image_tags):
    """Add the config-fingerprint annotation to every Deployment pod
    template in a (possibly multi-doc) manifest text, so changing any
    applied ConfigMap/Secret rolls the pods, and apply image_tags to its
    containers. Returns a Blob, or None if there are no Deployments."""
    docs = [d for d in decode_yaml_stream(content) if d != None]
    stamped = False
    for d in docs:
        if d.get("kind") in ("Deployment", "StatefulSet"):
            template = d.setdefault("spec", {}).setdefault("template", {})
            annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
            annotations[_CONFIG_HASH_ANNOTATION] = fingerprint
            _pin_images(template.get("spec") or {}, image_tags)
            stamped = True
    if not stamped:
        return None
    return encode_yaml_stream(docs)

def _add_labels(content, labels):
    """Add labels to metadata.labels of every object in a (possibly multi-doc)
    manifest text, keeping the labels an object already has."""
    docs = [d for d in decode_yaml_stream(content) if d != None]
    for d in docs:
        metadata = d.setdefault("metadata", {})
        existing = metadata.get("labels") or {}
        for key in labels:
            existing[key] = labels[key]
        metadata["labels"] = existing
    # A string, like the content it replaces: blob() below takes only a string.
    return str(encode_yaml_stream(docs))

def k8s_yaml_local(paths, local_overrides=None, substitutions=None, labels=None):
    """Apply k8s YAML with root-domain substitution and a pod-template
    fingerprint annotation covering every applied ConfigMap/Secret, including
    an optional gitignored ConfigMap of per-developer overrides (see module
    docstring).

    substitutions is an optional dict of literal token -> value replacements,
    applied after the root-domain pass so a token cannot be mangled by it. Use
    it for a value that varies per developer for some reason other than the
    root domain, or one read from elsewhere in the repo, such as a version
    pin.

    labels is an optional dict of label key -> value added to every object the
    call applies, the override ConfigMap included. An object Tilt applies
    beside a Pulumi program on purpose carries ol.mit.edu/yaml-by-design, which
    pulumi-app-apply.sh's leftover guard skips; a label written in the tracked
    YAML cannot reach the gitignored override file, so the call adds it."""
    rd = current_root_domain()

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
        # Caller-supplied literal replacements, applied after the root-domain
        # pass so a token cannot be mangled by it. Use these for values that
        # vary per developer and are not simply the root domain -- a hostname
        # that collides with something the developer already runs, say.
        for token in (substitutions or {}):
            content = content.replace(token, substitutions[token])
        if labels:
            content = _add_labels(content, labels)
        contents.append((p, content))

    fingerprint = _config_fingerprint(contents)
    image_tags = decode_json(os.environ.get(_PREBUILT_IMAGE_TAGS_ENV, "{}"))

    for p, content in contents:
        stamped = _stamp_workloads(content, fingerprint, image_tags)
        k8s_yaml(stamped if stamped != None else blob(content))


_WORKLOAD_RESTART = "local-dev/scripts/workload-restart.py"


def _label_selector(labels):
    return ",".join(["%s=%s" % (k, labels[k]) for k in sorted(labels.keys())])


def pulumi_app_rows(app, workers=[], beat=False, extra_rows={}, app_dir=None, src_path="/src", deps=[], namespace=None):
    """Give each Celery worker, beat and extra Deployment of a Pulumi-rendered
    app its own Tilt resource, named after the Deployment, beside <app>-app.

    app is the app's name: its ol.mit.edu/application label and the prefix of
    its Deployment names. namespace is where they run, when that is not the
    app's name. workers lists the program's Celery worker names, beat says
    whether it renders a beat Deployment, and extra_rows maps any other
    Deployment's name to labels selecting its pods. pulumi-app-apply.sh
    leaves these Deployments out of the app resource;
    tests/local_dev/test_tilt_workload_rows.py checks both against the
    rendered program and reads this call's arguments with ast, so they must
    be literals.

    A worker resource with app_dir copies that checkout to src_path in the
    running container and restarts Celery in place (workload-restart.py now);
    every other resource rolls its Deployment.

    deps should be the app resource's deps: a change there re-applies the app,
    which replaces the worker's pod (back on the image's code) whenever the
    change reaches its pod template, so a worker resource with a checkout
    shows it as pending. The resources are manual because running on that
    change could restart a pod Pulumi is about to replace; each still runs
    once at tilt up, after the app resource."""
    script = os.path.join(config.main_dir, _WORKLOAD_RESTART)
    namespace = namespace or app
    app_row = "%s-app" % app
    base = {"ol.mit.edu/application": app}
    beat_row = "%s-celery-beat" % app
    beat_labels = dict(base)
    beat_labels.update({"ol.mit.edu/component": "celery", "ol.mit.edu/worker-name": "beat"})

    def rollout_cmd(name):
        return [script, "rollout", app, name, "--namespace", namespace, "--app-row", app_row]

    rows = []
    for worker in workers:
        # The component's rule (celery_worker_deployment_name in
        # components/services/k8s.py): "_" becomes "-" in the name, not in the label.
        name = ("%s-%s-celery-worker" % (app, worker)).replace("_", "-")
        labels = dict(base)
        labels.update({"ol.mit.edu/component": "celery", "ol.mit.edu/worker-name": worker})
        if app_dir:
            cmd = [
                script, "now", app, name,
                "--namespace", namespace,
                "--selector", _label_selector(labels),
                "--container", "celery-worker",
                "--app-dir", app_dir,
                "--app-row", app_row,
                "--src-path", src_path,
            ]
            if beat:
                cmd += ["--beat-selector", _label_selector(beat_labels), "--beat-row", beat_row]
            rows.append((name, labels, cmd, deps))
        else:
            rows.append((name, labels, rollout_cmd(name), []))
    if beat:
        rows.append((beat_row, beat_labels, rollout_cmd(beat_row), []))
    for name in extra_rows:
        labels = dict(base)
        labels.update(extra_rows[name])
        rows.append((name, labels, rollout_cmd(name), []))

    for name, labels, cmd, row_deps in rows:
        k8s_custom_deploy(name, apply_cmd=cmd, delete_cmd=["true"], deps=row_deps)
        k8s_resource(
            name,
            labels=[app],
            resource_deps=[app_row],
            trigger_mode=TRIGGER_MODE_MANUAL,
            # The Deployment's uid changes when the app row recreates the
            # stack; the labels keep the row on the new pods.
            extra_pod_selectors=[labels],
        )
