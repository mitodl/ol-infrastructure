"""Count each graph's version objects and print one logfmt line per graph.

omnigraph 0.13's ``cleanup`` plans every object under a graph's ``_versions/``
directories before it deletes anything, at 0.16 to 0.22s each on S3, and it
never deletes a ``__manifest`` version (ModernRelay/omnigraph#644). QA and
Production run no scheduled cleanup, so these counts only grow, and nothing
else in the stack reports them. The Loki rules in
``infrastructure/grafana_alerting/log_rules/omnigraph_versions.py`` read the
lines this prints.

One line per graph, for example::

    event=omnigraph_versions graph=council manifest_versions=4680
    table_versions=4645 versions=9325

(wrapped here; it is one line).

``manifest_versions`` is ``__manifest/_versions/`` plus every
``__manifest/tree/<branch>/_versions/``: cleanup reads all of them and removes
none. ``table_versions`` is every node and edge table's ``_versions/``, which
cleanup can remove. ``versions`` is their sum, the count a cleanup plan pays
for.

Runs in the ``amazon/aws-cli`` image, whose system ``python3`` (3.9 on
Amazon Linux 2023) runs this and whose CLI lists the bucket with the pod's
IRSA credentials. Standard library only, and 3.9 syntax only.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections import Counter
from collections.abc import Iterable

EVENT = "omnigraph_versions"

# `aws s3 ls --recursive` prints "<date> <time> <size> <key>", with the key
# relative to the bucket.
_VERSION_KEY = re.compile(
    r"(?:^|/)graphs/(?P<graph>[^/]+)\.omni/(?P<dataset>.+)/_versions/[^/]+$"
)


def count_versions(listing: Iterable[str]) -> dict[str, Counter[str]]:
    """Tally ``manifest`` and ``table`` version objects per graph id.

    :param listing: lines of ``aws s3 ls --recursive`` output.
    :returns: graph id -> counter keyed ``manifest`` and ``table``.
    """
    counts: dict[str, Counter[str]] = {}
    for line in listing:
        fields = line.split(None, 3)
        if len(fields) != 4:  # noqa: PLR2004
            continue
        match = _VERSION_KEY.search(fields[3])
        if match is None:
            continue
        kind = "manifest" if match["dataset"].startswith("__manifest") else "table"
        counts.setdefault(match["graph"], Counter())[kind] += 1
    return counts


def render(counts: dict[str, Counter[str]]) -> list[str]:
    """One logfmt line per graph, sorted by graph id."""
    return [
        f"event={EVENT} graph={graph}"
        f" manifest_versions={tally['manifest']}"
        f" table_versions={tally['table']}"
        f" versions={tally['manifest'] + tally['table']}"
        for graph, tally in sorted(counts.items())
    ]


def main(env: dict[str, str]) -> int:
    """List ``$OMNIGRAPH_STORAGE_ROOT/graphs/`` and print the counts.

    The CLI's stderr is left to the pod's, so a credential or permission
    error reaches the log rather than only an exit status.

    :returns: 0, or 1 when the listing names no graph at all.
    :raises subprocess.CalledProcessError: when the listing fails.
    """
    graphs_uri = env["OMNIGRAPH_STORAGE_ROOT"].rstrip("/") + "/graphs/"
    args = ["aws", "s3", "ls", "--recursive", graphs_uri]
    listing = subprocess.run(  # noqa: S603
        args,
        stdout=subprocess.PIPE,
        text=True,
        check=False,
    )
    # `aws s3 ls` exits 1 with no output on a prefix holding no objects,
    # which is what a wrong root looks like. Any other failure is the CLI's.
    if listing.returncode != 0 and (listing.returncode != 1 or listing.stdout):
        raise subprocess.CalledProcessError(listing.returncode, args)
    counts = count_versions(listing.stdout.splitlines())
    if not counts:
        # Printing nothing would read as NoData to the rules, i.e. fine.
        sys.stderr.write(f"no graph versions found under {graphs_uri}\n")
        return 1
    for line in render(counts):
        sys.stdout.write(line + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(dict(os.environ)))
