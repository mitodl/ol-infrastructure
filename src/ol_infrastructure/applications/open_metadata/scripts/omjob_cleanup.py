"""Delete finished OMJobs older than the retention window.

The omjob-operator never deletes an OMJob. ``ttlSecondsAfterFinished`` only
removes its pods, and the CronOMJob ``successfulJobsHistoryLimit`` and
``failedJobsHistoryLimit`` fields are in the CRD and the model but nothing in
the 2.0.7 operator reads them. Every run therefore leaves an OMJob behind, each
carrying its CronOMJob's full pod spec (the ingestion script included), and the
operator's informer holds all of them on its heap.

Stdlib only, so it runs on a stock Python image with the pod's own
ServiceAccount token.
"""

import json
import os
import ssl
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

SERVICE_ACCOUNT_DIR = Path("/var/run/secrets/kubernetes.io/serviceaccount")
TERMINAL_PHASES = frozenset({"Succeeded", "Failed"})
PAGE_SIZE = 100
HTTP_TIMEOUT_SECONDS = 60


def main() -> None:
    """Delete this namespace's Succeeded and Failed OMJobs past retention."""
    namespace = (SERVICE_ACCOUNT_DIR / "namespace").read_text().strip()
    token = (SERVICE_ACCOUNT_DIR / "token").read_text().strip()
    context = ssl.create_default_context(cafile=SERVICE_ACCOUNT_DIR / "ca.crt")
    base_url = (
        f"https://{os.environ['KUBERNETES_SERVICE_HOST']}:"
        f"{os.environ['KUBERNETES_SERVICE_PORT']}"
        f"/apis/pipelines.openmetadata.org/v1/namespaces/{namespace}/omjobs"
    )
    cutoff = datetime.now(UTC) - timedelta(days=int(os.environ["OMJOB_RETENTION_DAYS"]))

    def call(method: str, url: str) -> dict[str, Any]:
        request = urllib.request.Request(  # noqa: S310 - in-cluster API server URL
            url, method=method, headers={"Authorization": f"Bearer {token}"}
        )
        with urllib.request.urlopen(  # noqa: S310 - in-cluster API server URL
            request, context=context, timeout=HTTP_TIMEOUT_SECONDS
        ) as response:
            return json.load(response)

    expired: list[str] = []
    continue_token = ""
    while True:
        query = urllib.parse.urlencode({"limit": PAGE_SIZE, "continue": continue_token})
        page = call("GET", f"{base_url}?{query}")
        for omjob in page["items"]:
            # An OMJob the operator has not reconciled yet has no status at all.
            status = omjob.get("status") or {}
            if status.get("phase") not in TERMINAL_PHASES:
                continue
            completed = datetime.fromisoformat(status["completionTime"])
            if completed < cutoff:
                expired.append(omjob["metadata"]["name"])
        continue_token = page["metadata"].get("continue", "")
        if not continue_token:
            break

    for name in expired:
        call("DELETE", f"{base_url}/{name}")
    print(  # noqa: T201
        f"deleted {len(expired)} OMJobs in {namespace} that finished before "
        f"{cutoff:%Y-%m-%dT%H:%M:%SZ}"
    )


if __name__ == "__main__":
    main()
