import os

from bridge.settings.openedx.version_matrix import OpenEdxSupportedRelease

WEB_NODE_TYPE = "web"
WORKER_NODE_TYPE = "worker"
node_type = os.environ.get("NODE_TYPE", WEB_NODE_TYPE)
EDX_RELEASE: OpenEdxSupportedRelease = os.environ.get("EDX_RELEASE_NAME", "master")
