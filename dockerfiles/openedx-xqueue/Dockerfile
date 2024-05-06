FROM ubuntu:focal

RUN apt update && \
  apt-get install -y software-properties-common && \
  apt-add-repository -y ppa:deadsnakes/ppa && apt-get update && \
  apt-get install git-core language-pack-en python3-pip libmysqlclient-dev ntp libssl-dev python3.8-dev python3.8-venv pkg-config -qy && \
  rm -rf /var/lib/apt/lists/*

ARG OPENEDX_COMMON_VERSION=open-release/olive.master
ENV VIRTUAL_ENV=/venv
ENV DJANGO_SETTINGS_MODULE=xqueue.production
RUN python3.8 -m venv $VIRTUAL_ENV
ENV PATH="$VIRTUAL_ENV/bin:$PATH"

RUN locale-gen en_US.UTF-8
ENV LANG en_US.UTF-8
ENV LANGUAGE en_US:en
ENV LC_ALL en_US.UTF-8

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
