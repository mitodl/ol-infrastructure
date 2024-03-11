FROM python:3.8-slim

ARG APP_USER_ID=1000
RUN apt update && \
  apt install -y git mariadb-client default-libmysqlclient-dev build-essential pkg-config && \
  apt clean
RUN useradd --home-dir /app --create-home --shell /bin/bash --uid ${APP_USER_ID} app
USER ${APP_USER_ID}
ARG EDX_NOTES_GIT_BRANCH=master
WORKDIR /app/edx-notes-api
ENV EDX_NOTES_CONFIG_ROOT=/app/edx-notes-api/
ENV DJANGO_SETTINGS_MODULE notesserver.settings.yaml_config
ENV PATH=/app/.local/bin:$PATH

RUN git clone https://github.com/edx/edx-notes-api --branch ${EDX_NOTES_GIT_BRANCH} --depth 1 /app/edx-notes-api
RUN pip install --no-cache-dir -r requirements/base.txt

EXPOSE 8000
CMD gunicorn --workers=2 --name notes --bind=0.0.0.0:8000 --max-requests=1000 notesserver.wsgi:application
