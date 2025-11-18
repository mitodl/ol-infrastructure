ARG PYTHON_VERSION=3.8
FROM python:${PYTHON_VERSION}-slim

ARG APP_USER_ID=1000
RUN apt update && \
  apt install -y git mariadb-client default-libmysqlclient-dev build-essential pkg-config && \
  apt clean
RUN useradd --home-dir /app --create-home --shell /bin/bash --uid ${APP_USER_ID} app
USER ${APP_USER_ID}
ARG EDX_NOTES_GIT_BRANCH=master
WORKDIR /app/edx-notes-api
ENV PATH=/app/.local/bin:$PATH

RUN git clone https://github.com/edx/edx-notes-api --branch ${EDX_NOTES_GIT_BRANCH} --depth 1 /app/edx-notes-api
RUN pip install --no-cache-dir -r requirements/base.txt

# Copy custom settings module that loads from environment variables
COPY --chown=app:app env_config.py /app/edx-notes-api/notesserver/settings/env_config.py

EXPOSE 8000
CMD gunicorn --workers=2 --name notes --bind=0.0.0.0:8000 --max-requests=1000 notesserver.wsgi:application
