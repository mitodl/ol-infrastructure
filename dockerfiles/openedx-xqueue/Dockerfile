ARG PYTHON_VERSION=3.11
FROM python:${PYTHON_VERSION}-slim-trixie
ARG OPENEDX_COMMON_VERSION=release/ulmo
ENV DJANGO_SETTINGS_MODULE=xqueue.env_config

RUN apt-get update && \
  apt-get install --no-install-recommends -y git-core mariadb-client default-libmysqlclient-dev build-essential openntpd libssl-dev pkg-config && \
  rm -rf /var/lib/apt/lists/* && \
  useradd -m --shell /bin/false app && \
  mkdir -p /edx/app/log/ && \
  touch /edx/app/log/edx.log && \
  chown app:app /edx/app/log/edx.log && \
  git clone https://github.com/openedx/xqueue.git --branch $OPENEDX_COMMON_VERSION --depth 1 /edx/app/xqueue
WORKDIR /edx/app/xqueue
RUN pip install --no-cache-dir wheel && pip install --no-cache-dir -r requirements.txt

# Copy Django settings module for environment variable configuration
COPY --chown=app:app env_config.py xqueue/env_config.py

USER app

EXPOSE 8040
CMD ["gunicorn", "-c", "/edx/app/xqueue/xqueue/docker_gunicorn_configuration.py", "--bind=0.0.0.0:8040", "--workers", "2", "--max-requests=1000", "xqueue.wsgi:application"]
