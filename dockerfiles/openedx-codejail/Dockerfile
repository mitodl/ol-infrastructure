ARG PYTHON_VERSION=3.12
FROM python:${PYTHON_VERSION}-slim-bookworm
SHELL ["/bin/bash", "-c"]

ENV DEBIAN_FRONTEND=noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN=true
RUN apt-get update && apt-get install --no-install-recommends -y python3-virtualenv python3-pip git sudo libxslt-dev && apt-get clean

# Define Environment Variables
ENV CODEJAIL_GROUP=sandbox
ENV CODEJAIL_SANDBOX_CALLER=debian
ENV CODEJAIL_USER=sandbox
ENV CODEJAIL_VENV=/sandbox/venv
ARG OPENEDX_BRANCH=master
ARG OPENEDX_RELEASE
ENV OPEN_EDX_RELEASE=$OPENEDX_RELEASE
ENV OPEN_EDX_BRANCH=$OPENEDX_BRANCH

RUN virtualenv -p python${PYTHON_VERSION} --always-copy $CODEJAIL_VENV
ENV PATH="$CODEJAIL_VENV/bin:$PATH"

# Create Sandbox user & group
RUN addgroup $CODEJAIL_GROUP
RUN adduser --disabled-login --disabled-password $CODEJAIL_USER --ingroup $CODEJAIL_GROUP

# Switch to non root user inside Docker container
RUN addgroup debian
RUN adduser --disabled-login --disabled-password debian --ingroup debian

# Give Ownership of sandbox env to sandbox group and user
RUN chown -R $CODEJAIL_USER:$CODEJAIL_GROUP $CODEJAIL_VENV

WORKDIR /codejail
RUN git clone https://github.com/eduNEXT/codejailservice/ --branch main --depth 1 /codejail

# Install dependencies
RUN pip install --no-cache-dir -r requirements/base.txt  && \
    pip install --no-cache-dir gunicorn && \
    source $CODEJAIL_VENV/bin/activate && \
    pip install --no-cache-dir -r https://raw.githubusercontent.com/openedx/edx-platform/master/requirements/edx-sandbox/releases/${OPEN_EDX_RELEASE:-../base}.txt  && \
    deactivate

# Setup sudoers file
COPY sudoers-file/01-sandbox /etc/sudoers.d/01-sandbox

# Change Sudoers file permissions
RUN chmod 0440 /etc/sudoers.d/01-sandbox

# Change Repo ownership
RUN chown -R debian:debian ../codejail

# Switch to debian user
USER debian
CMD gunicorn -b 0.0.0.0:8000 --workers 2 --max-requests=1000 wsgi
