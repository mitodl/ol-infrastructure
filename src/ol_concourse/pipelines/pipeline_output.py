"""Serialize a Pipeline with a `user_data` key merged in.

Concourse's pipeline-config schema already accepts an opaque top-level `user_data`
field (concourse/concourse#9489, merged 2026-03-25 -- shipped in our pinned 8.3.0,
see CONCOURSE_VERSION in src/bridge/lib/versions.py). The web UI does not render it
yet: that lands with concourse/concourse#9661 ("Add pipeline-level description
field", merged 2026-09-01), which is not in a release as of this writing. Setting
`user_data` now costs nothing -- Concourse already stores and returns it through the
set-pipeline/get-pipeline cycle -- and it starts rendering as an info page the moment
we bump CONCOURSE_VERSION past whatever release ships #9661.

#9661's own rendering rule: `user_data` as an object with a string `description` key
renders that key as markdown, with the remaining keys rendered as YAML below it. So
every call site here should set `description` plus any other metadata worth carrying
(e.g. `team`, `category`).

The installed `ol-concourse` package's `Pipeline` model has no typed `user_data`
field yet -- that also waits on a release of the separate mitodl/ol-concourse
package -- and uses `extra="forbid"`, so `user_data` can't be passed into
`Pipeline(...)` directly today. This merges it into the model's own canonical
serialization rather than hand-building any part of the pipeline as raw JSON.
"""

import json
from typing import Any

from ol_concourse.lib.models.pipeline import Pipeline


def pipeline_json_with_user_data(pipeline: Pipeline, user_data: dict[str, Any]) -> str:
    """Serialize a Pipeline to JSON with a `user_data` key merged in.

    :param pipeline: The built Pipeline.
    :param user_data: Written as-is under the top-level `user_data` key.

    :returns: The JSON string, ready to write to `definition.json` and echo to stdout.
    """
    payload = json.loads(pipeline.model_dump_json())
    payload["user_data"] = user_data
    return json.dumps(payload, indent=2)
