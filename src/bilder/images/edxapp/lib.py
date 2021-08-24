import os

WEB_NODE_TYPE = "web"
WORKER_NODE_TYPE = "worker"
node_type = os.environ.get("NODE_TYPE", WEB_NODE_TYPE)
