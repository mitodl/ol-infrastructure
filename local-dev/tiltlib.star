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
_LIVE_UPDATE_MARKER = "/tmp/tilt-synced"


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

def checkout_live_update(steps):
    """A docker_build's live_update for an image that several Deployments
    run from a synced checkout: steps (the app's fall_back_on and sync)
    between two additions.

    initial_sync() copies the whole checkout into a container when it starts
    or restarts. Tilt otherwise replays only the saves made since the image
    was last built, and one resource can rebuild a shared image without
    redeploying the others: a container still on the older image would come
    back from a restart missing earlier saves.

    That copy adds and overwrites files; Tilt removes none during it. A file
    deleted from the checkout after a container's image was built is in the
    container again after a restart, until the image is rebuilt.

    The last step tells a container waiting in live_update_wait() that the
    copy is in; Tilt runs it after every copy. The containers wait when the
    image is in k8s_yaml_local's live_update_images or, for a Pulumi-rendered
    app, when its apply_env passes LOCAL_DEV_LIVE_UPDATE_WAIT."""
    return [initial_sync()] + steps + [run("touch " + _LIVE_UPDATE_MARKER)]

def live_update_wait():
    """The words to put in front of a container's command so the command
    starts after Tilt's copy of the checkout, not during it.

    A container starts on its image's files, and Tilt copies the checkout in
    while the process is already importing. A process that reads some modules
    before the copy and others after fails on the mix; the restarted
    container is back on the image's files, so it fails the same way again.
    With no copy in 30 s (Tilt is not running, say) the command starts on the
    image's files."""
    script = str(read_file(os.path.join(config.main_dir, "local-dev/scripts/wait-for-live-update.sh")))
    return ["sh", "-c", script, "wait-for-live-update", _LIVE_UPDATE_MARKER, "30"]

def _start_after_live_update(pod_spec, images):
    """Put live_update_wait() in front of the command of every container
    whose image is in images. Init containers are left alone: Tilt copies
    nothing into them."""
    for c in pod_spec.get("containers") or []:
        if _image_name(c.get("image", "")) in images and c.get("command"):
            c["command"] = live_update_wait() + c["command"]

def _stamp_workloads(content, fingerprint, image_tags, live_update_images):
    """Add the config-fingerprint annotation to every Deployment pod
    template in a (possibly multi-doc) manifest text, so changing any
    applied ConfigMap/Secret rolls the pods, apply image_tags to its
    containers and hold the commands of those running live_update_images.
    Returns a Blob, or None if there are no Deployments."""
    docs = [d for d in decode_yaml_stream(content) if d != None]
    stamped = False
    for d in docs:
        if d.get("kind") in ("Deployment", "StatefulSet"):
            template = d.setdefault("spec", {}).setdefault("template", {})
            annotations = template.setdefault("metadata", {}).setdefault("annotations", {})
            annotations[_CONFIG_HASH_ANNOTATION] = fingerprint
            _pin_images(template.get("spec") or {}, image_tags)
            _start_after_live_update(template.get("spec") or {}, live_update_images)
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

def k8s_yaml_local(paths, local_overrides=None, substitutions=None, labels=None, live_update_images=()):
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
    YAML cannot reach the gitignored override file, so the call adds it.

    live_update_images names the images Tilt builds with a
    checkout_live_update(). Deployment containers running one start their
    command through live_update_wait()."""
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
        stamped = _stamp_workloads(content, fingerprint, image_tags, live_update_images)
        k8s_yaml(stamped if stamped != None else blob(content))


def pulumi_app_workloads(app, app_resource, workloads, image):
    """Give Deployments of a Pulumi-rendered app Tilt resources of their own,
    each named after its Deployment, so each has its own status and log.

    app is the name pulumi-app-apply.sh takes: the program's directory, the
    namespace and the Tilt label. app_resource is the Tilt resource that runs
    that script. image is the image the app's docker_build builds, or None
    when there is no checkout to build from.

    workloads maps a Deployment's name to {"labels": ..., "container": ...}.
    labels must select that Deployment's pods and no others in the cluster.
    container, where given, names the container that runs the app's image:
    the resource then depends on the image, so saves sync into the container
    and a click rebuilds the image and rolls the Deployment onto it. Without
    a container (or an image) the resource only shows the Deployment.

    An app's Tiltfile declares the mapping as a literal named WORKLOADS
    (tests/local_dev/test_tilt_workloads.py reads it and checks it against
    the rendered program), appends list(WORKLOADS) to the app resource's
    apply_cmd so pulumi-app-apply.sh leaves those Deployments out of it, and
    passes WORKLOADS here."""
    script = os.path.join(config.main_dir, "local-dev/scripts/pulumi-workload-apply.sh")
    for name in workloads:
        container = workloads[name].get("container")
        labels = workloads[name]["labels"]
        k8s_custom_deploy(
            name,
            apply_cmd=[script, app, name] + ([container] if container else []),
            # A click is a force update, which runs the delete before the
            # apply. The Deployment is Pulumi's to delete, so the delete
            # removes its pods: a click then restarts the process even when
            # the rebuilt image has the tag the Deployment already runs,
            # which the apply's `kubectl set image` would leave alone. Tilt's
            # other applies (at `tilt up`, after a rebuild) run no delete.
            # Disabling the resource runs it too, which restarts the pods:
            # the Deployment stays until the app's resource is disabled.
            delete_cmd=[
                "kubectl", "--context", "local-dev", "-n", app, "delete", "pod", "--wait=false",
                "-l", ",".join(["%s=%s" % (key, labels[key]) for key in labels]),
            ],
            deps=[],
            image_deps=[image] if container and image else [],
        )
        k8s_resource(
            name,
            labels=[app],
            resource_deps=[app_resource],
            # The Deployment's uid changes when the app resource recreates
            # the stack; the labels keep this resource on the new pods.
            extra_pod_selectors=[labels],
        )
