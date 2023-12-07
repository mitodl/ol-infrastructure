FROM apachesuperset.docker.scarf.sh/apache/superset:${SUPERSET_TAG:-latest}
COPY requirements.txt requirements.txt
COPY superset_config.py /app
RUN pip install --no-cache-dir -r requirements.txt
