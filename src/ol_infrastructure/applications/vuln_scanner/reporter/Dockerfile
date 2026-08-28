# Multi-stage build: `mitodl/ol-python-base` (dockerfiles/ol-python-base/Dockerfile)
# has a shell and a package manager -- fine for the build stage, but gwarek's
# own runtime image deliberately ships neither ("the DHI runtime image this
# runs on ships no shell", applications/gwarek/__main__.py:723), and this
# reporter carries real S3-write + Security-Hub-import credentials, so it
# should match that posture rather than a single-stage build that quietly
# doesn't.
#
# Both images are pinned by digest, not floating tags, because they must stay
# on the SAME underlying Debian release for the copied CPython build below to
# even run: ol-python-base:3.14 currently tracks Debian 13 (trixie), so the
# final stage must be distroless's debian13 variant, not debian12 -- verified
# by hand (a mismatched pair fails at runtime with a glibc symbol-version
# error, not a build-time error, so this only surfaces when the container
# actually starts). Bumping either image's tag/digest requires re-checking
# this pairing, not just editing the FROM line.
#
# 3.14 rather than 3.12 (this project's own pyproject.toml requirement)
# to match the newest precedent already in this repo for small standalone
# tool images (release_bot, kubewatch_webhook_handler both use
# python:3.14-slim) -- nothing about this reporter ties it to an older
# runtime, so there's no reason to pick one with a shorter support window.
FROM mitodl/ol-python-base@sha256:0eedc615b6d7c3040cb14f5bdc186cb1d41ac8663c471666f8edeb748b8bacf1 AS builder

WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

FROM gcr.io/distroless/base-debian13@sha256:9ef50bca108839d5986e4d84b7f7b2d79024c9293b7c35b162c6c55485bd5868

# distroless/base ships no language runtime at all (that's the point -- no
# shell, no package manager, minimal attack surface), so the interpreter
# itself has to come from the builder stage alongside the venv it built.
# Copying only /opt/venv (as an earlier, untested version of this Dockerfile
# did) fails at container start: the venv's own python binary is a symlink
# chain out to these exact paths in the builder image, which doesn't exist
# in the final stage without this.
COPY --from=builder /usr/local/lib/python3.14 /usr/local/lib/python3.14
COPY --from=builder /usr/local/lib/libpython3.14.so.1.0 /usr/local/lib/libpython3.14.so.1.0
COPY --from=builder /usr/local/bin/python3.14 /usr/local/bin/python3.14
COPY --from=builder /usr/local/bin/python3 /usr/local/bin/python3
COPY --from=builder /opt/venv /opt/venv

COPY reporter.py /app/reporter.py
WORKDIR /app

# No shell to chain commands through, and no CMD -- ENTRYPOINT calls the
# venv's own interpreter directly, mirroring the same constraint gwarek's
# migration job already works around (`__main__.py:723-726`).
ENTRYPOINT ["/opt/venv/bin/python3", "reporter.py"]
