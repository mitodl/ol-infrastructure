ARG PYTHON_VERSION=3.8
FROM python:${PYTHON_VERSION}-slim-bookworm

RUN apt update && \
  apt-get install -y software-properties-common && \
  apt-get install git-core mariadb-client default-libmysqlclient-dev ntp libssl-dev pkg-config -qy && \
  rm -rf /var/lib/apt/lists/*

ARG OPENEDX_COMMON_VERSION=open-release/quince.master
ENV DJANGO_SETTINGS_MODULE=xqueue.production

RUN useradd -m --shell /bin/false app
RUN mkdir -p /edx/app/log/
RUN touch /edx/app/log/edx.log
RUN chown app:app /edx/app/log/edx.log

RUN git clone https://github.com/openedx/xqueue.git --branch $OPENEDX_COMMON_VERSION --depth 1 /edx/app/xqueue
WORKDIR /edx/app/xqueue
RUN pip install wheel && pip install -r requirements.txt

USER app

EXPOSE 8040
CMD gunicorn -c /edx/app/xqueue/xqueue/docker_gunicorn_configuration.py --bind=0.0.0.0:8040 --workers 2 --max-requests=1000 xqueue.wsgi:application
