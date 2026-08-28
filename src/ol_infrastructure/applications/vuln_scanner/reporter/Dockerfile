# Shared hardened base (dockerfiles/ol-python-base/Dockerfile): slim Python,
# uv preinstalled, non-root `mitodl` user, /opt/venv as UV_PROJECT_ENVIRONMENT.
# Not pinned by digest yet -- pin once this image has actually been built and
# pushed and a real digest exists to pin to; do not fabricate one.
FROM mitodl/ol-python-base:3.12

WORKDIR /src

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY reporter.py .

USER mitodl

CMD ["python", "reporter.py"]
