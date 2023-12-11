FROM apachesuperset.docker.scarf.sh/apache/superset:${SUPERSET_TAG:-latest}
COPY --link requirements.in requirements.txt
COPY --link pythonpath /app/pythonpath
USER root
RUN pip install --no-cache-dir -r requirements.txt
USER superset
