FROM apache/superset:${SUPERSET_TAG:-latest}
COPY --link requirements.in requirements.txt
COPY --link pythonpath /app/pythonpath
COPY static/assets/images/ol-data-platform-logo.svg /app/superset/static/assets/images/ol-data-platform-logo.svg
USER root
RUN pip install --no-cache-dir -r requirements.txt
USER superset
