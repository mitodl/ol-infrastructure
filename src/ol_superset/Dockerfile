ARG SUPERSET_TAG=5.0.0
FROM apache/superset:${SUPERSET_TAG}
COPY --link requirements.txt /tmp/requirements.txt
COPY --link pythonpath /app/pythonpath
COPY static/assets/images/ol-data-platform-logo.svg /app/superset/static/assets/images/ol-data-platform-logo.svg
USER root
WORKDIR /app
RUN uv pip install --no-cache-dir -r /tmp/requirements.txt
USER superset
