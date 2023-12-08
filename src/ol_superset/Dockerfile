FROM apachesuperset.docker.scarf.sh/apache/superset:${SUPERSET_TAG:-latest}
COPY requirements.txt requirements.txt
RUN pip install --no-cache-dir -r requirements.txt
COPY superset_config.py /app
COPY vault /app
