#!/usr/bin/env python3
"""Print the Kubernetes objects a Pulumi checkpoint still shows as being created.

Reads `pulumi stack export` JSON on stdin and prints one line of space-separated
``kind/name`` tokens for every pending ``creating`` operation, or nothing. A run
killed between the API call and the resource's await (Tilt's apply timeout,
Ctrl-C) leaves exactly these: the object exists in the cluster but not in the
state, so the next `pulumi up` fails on it with "already exists".
"""

import json
import sys

deployment = json.load(sys.stdin).get("deployment") or {}
objects = []
for op in deployment.get("pending_operations") or []:
    # Providers, the Stack and ol: components have no object to delete.
    if op.get("type") != "creating" or not op["resource"]["type"].startswith(
        "kubernetes:"
    ):
        continue
    kind = op["resource"]["type"].rsplit(":", 1)[-1].lower()
    objects.append(f"{kind}/{op['resource']['inputs']['metadata']['name']}")
if objects:
    print(" ".join(objects))  # noqa: T201
