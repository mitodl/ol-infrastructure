# version_pins

**Generated. Do not edit by hand.**

One extensionless file per constant in [`../versions.py`](../versions.py),
containing only that constant's value. `src/bridge/lib/sync_version_pins.py`
writes them; a pre-commit hook runs it, and
`tests/ol_concourse/test_versions_map.py` fails if they ever drift.

## Why this exists

`versions.py` stays the single readable record of every version we run --
grouped, annotated, and Renovate-managed in one place. But Concourse `git`
resources trigger on *file* paths, so a pipeline that cares about one line of
that file had to watch the entire file. Every unrelated bump anywhere in it
re-triggered that pipeline's AMI/image build and its whole CI -> QA ->
Production deploy chain.

Watching `version_pins/KEYCLOAK_VERSION` instead means the Keycloak pipeline
re-triggers when Keycloak moves and stays quiet otherwise. Reflowing a comment
in `versions.py` now triggers nothing at all.

Which pipeline watches which pins is recorded in
`src/ol_concourse/pipelines/versions_map.py`, derived from static analysis of
what each Pulumi project and Packer image actually imports.

## Changing a version

Edit `versions.py` as always, then let pre-commit regenerate this directory
(`pre-commit run --all-files`, or `python src/bridge/lib/sync_version_pins.py`).
Renovate only ever touches `versions.py`; pre-commit.ci regenerates these files
inside the Renovate PR, so both land in the same commit.
