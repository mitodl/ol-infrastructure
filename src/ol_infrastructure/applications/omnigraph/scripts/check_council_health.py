"""Authenticated synthetic probe: prove `council` answers real queries.

It sends TWO queries, and the second one exists because the first cannot see
what broke CI on 2026-09-08: an ordinary read and a full-text (`search()`)
read fail independently, so a probe that only does the former reports healthy
while every BM25 caller is being refused. See `SEARCH_PROBE_QUERY` below.

Carved out of tk-observability-for-shared-witan-service-ad3dba item 1
(tk-an-authenticated-synthetic-probe-for-council-to--e89d8f). Two alert rules
already cover a lost `council` graph from opposite directions —
`WitanToolCallErrorRate` (needs live traffic) and `WitanGraphQuarantined`
(needs a pod restart) — and a real window sits between them: `council` lost
during a quiet period with no restart goes unreported until someone tries to
use the service, which for a shared agent memory graph can be hours.

Neither existing health check can close that window, and both should stay as
they are:
  - omnigraph's `/healthz` is flat and never auth-gated by design.
  - witan's own probe answers from process state alone — a probe that checks
    the data tier converts backend SLOWNESS into frontend DEATH, which is
    exactly what took the service down on 2026-08-12.

So this is a deliberately separate, out-of-band, AUTHENTICATED caller: it runs
as its own CronJob, in its own pod, with its own narrowly-scoped Cedar
identity (`svc-witan-probe` — read + invoke_query on the memory graph only,
nothing else), and does the one thing neither existing check can: run a real
query against `council` and see whether it comes back.

WHY THIS NEEDS NO NEW ALERT RULE

A failing run IS the signal. `WitanScheduledJobNeverSucceeded` and
`eks_general.py`'s `WorkloadJobFailed*` already alert on a CronJob whose Job
fails — this script just needs to exit non-zero for a real failure and exit 0
for a real success, cleanly, so those existing rules have something honest to
key off. The CronJob's own name is added to `eks_general.py`'s fast staleness
bucket, for the "stopped running entirely" case those Job-failure rules
cannot see.

WHY STDLIB-ONLY

Same reason as `sync_actor_tokens.py`: one HTTP call needs nothing installed,
so this ships as a ConfigMap against `python:3.12-slim` rather than a built
image — a change here is a change to this stack and nothing else.

WHY A RAW HTTP CALL RATHER THAN THE `witan_core` CLIENT

The whole point is to exercise the same wire contract every real caller uses
(`POST /graphs/<id>/query`, `witan_core.omnigraph_http.PooledTransport.query`)
without depending on `witan_core` being installed — installing it here would
turn this ConfigMap back into an image-build problem. The query text below is
deliberately trivial and read-only.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.request
from typing import Any, cast

LOG = logging.getLogger("check-council-health")

# One bounded read against a node type every environment's memory graph
# schema declares. No parameters, so there is nothing for a caller to get
# wrong — the whole point is to fail on the SERVER'S answer, not on this
# script's own input.
PROBE_QUERY = """
query council_probe() {
    match { $m: Memory }
    return { $m.slug }
    limit 1
}
"""

# A second bounded read that goes through the FULL-TEXT path specifically.
#
# The plain query above cannot see a dead BM25 index, and on 2026-09-08 that
# was not hypothetical: CI was skipped in the 2026-09-01 Lance 11 analyzer
# rebuild, every `search()`/`bm25()` query there was refused with HTTP 409
# `full_text_index_rebuild_required`, and this probe went on passing every 15
# minutes because `match { $m: Memory }` is an ordinary read. The
# rebuild-required guard is deliberately narrow — "ordinary reads remain
# available; do not return a partial indexed result" — and that narrowness is
# exactly what made the existing probe blind to it. `check_omnigraph_format.py`
# stays green through an analyzer bump too, and correctly so: the on-disk
# format does not move. Nothing else watches this.
#
# ★ IT DELIBERATELY DOES NOT ASSERT ON THE HIT COUNT. A search matching zero
# rows is a legitimate answer — an empty or freshly-cut-over graph would fail
# such an assertion forever, for no defect. What this probe asserts is that the
# full-text path ANSWERS AT ALL, which is precisely the thing that breaks: the
# failure mode is a refusal (HTTP 409), not a wrong result, so `run_probe`'s
# existing non-2xx handling turns it into a non-zero exit with no new
# machinery. Keep the literal inline rather than parameterising it, for the
# same reason the query above takes no parameters.
SEARCH_PROBE_QUERY = """
query council_search_probe() {
    match {
        $m: Memory
        search($m.content, "witan")
    }
    return { $m.slug }
    limit 1
}
"""

HTTP_TIMEOUT_SECONDS = 15


class ProbeError(Exception):
    """A condition that must exit the run non-zero."""


def run_probe(
    server_addr: str, graph_id: str, token: str, query: str = PROBE_QUERY
) -> dict[str, Any]:
    """POST a probe query and return the decoded JSON body on success.

    Raises :class:`ProbeError` for anything that means `council` did not
    answer: an unreachable server, a non-2xx response, or a 2xx body that
    is not the JSON shape a query response actually takes. That last case
    matters as much as the network failures — a 200 from a misconfigured
    proxy in front of the real server would otherwise read as success.
    """
    url = f"{server_addr.rstrip('/')}/graphs/{graph_id}/query"
    payload = json.dumps({"query": query, "params": {}}).encode()
    request = urllib.request.Request(  # noqa: S310 - server_addr is our own config
        url,
        method="POST",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(  # noqa: S310 - server_addr is our own config
            request, timeout=HTTP_TIMEOUT_SECONDS
        ) as response:
            status = response.status
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        msg = f"POST {url} failed: HTTP {exc.code} {detail}"
        raise ProbeError(msg) from exc
    except urllib.error.URLError as exc:
        msg = f"POST {url} failed: {exc.reason}"
        raise ProbeError(msg) from exc
    except TimeoutError as exc:
        msg = f"POST {url} timed out after {HTTP_TIMEOUT_SECONDS}s"
        raise ProbeError(msg) from exc

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError as exc:
        msg = (
            f"POST {url} returned HTTP {status} with a non-JSON body "
            f"({body[:200]!r}): {exc}"
        )
        raise ProbeError(msg) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("rows"), list):
        msg = (
            f"POST {url} returned HTTP {status} with an unexpected body shape "
            f"('rows' missing or not a list): {parsed!r}"
        )
        raise ProbeError(msg)
    return parsed


def main() -> int:
    """Run the probe once and exit non-zero on any failure to answer."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    server_addr = os.environ.get("OMNIGRAPH_SERVER_ADDR")
    graph_id = os.environ.get("OMNIGRAPH_GRAPH_ID", "council")
    token = os.environ.get("OMNIGRAPH_BEARER_TOKEN")
    missing = [
        name
        for name, value in (
            ("OMNIGRAPH_SERVER_ADDR", server_addr),
            ("OMNIGRAPH_BEARER_TOKEN", token),
        )
        if not value
    ]
    if missing:
        LOG.error("missing required env var(s): %s", ", ".join(missing))
        return 1

    try:
        # `missing` being empty is what guarantees these are str, not None;
        # cast rather than assert since this is not a runtime invariant worth
        # enforcing twice, just a fact the type checker cannot see through
        # the comprehension above.
        result = run_probe(cast(str, server_addr), graph_id, cast(str, token))
        search_result = run_probe(
            cast(str, server_addr),
            graph_id,
            cast(str, token),
            query=SEARCH_PROBE_QUERY,
        )
    except ProbeError as exc:
        # Not `.exception()`: `exc` is already a fully-formatted, known
        # failure message (a classified HTTP/JSON condition), not an
        # unexpected exception whose traceback would add anything here.
        LOG.error("council-health probe failed: %s", exc)  # noqa: TRY400
        return 1

    LOG.info(
        "council-health probe OK: graph=%s row_count=%s search_row_count=%s",
        graph_id,
        result.get("row_count", len(result.get("rows", []))),
        search_result.get("row_count", len(search_result.get("rows", []))),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
