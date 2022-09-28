FROM ubuntu:focal
SHELL ["/bin/bash", "-c"]

ENV DEBIAN_FRONTEND=noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN=true
RUN apt-get update && apt-get upgrade -y
RUN apt-get install -y vim python3-virtualenv python3-pip git sudo libxslt-dev

# Define Environment Variables
ENV CODEJAIL_GROUP=sandbox
ENV CODEJAIL_SANDBOX_CALLER=ubuntu
ENV CODEJAIL_USER=sandbox
ENV CODEJAIL_VENV=/sandbox/venv/

RUN virtualenv -p python3.8 --always-copy $CODEJAIL_VENV
ENV PATH="$CODEJAIL_VENV/bin:$PATH"

# Create Sandbox user & group
RUN addgroup $CODEJAIL_GROUP
RUN adduser --disabled-login --disabled-password $CODEJAIL_USER --ingroup $CODEJAIL_GROUP

# Switch to non root user inside Docker container
RUN addgroup ubuntu
RUN adduser --disabled-login --disabled-password ubuntu --ingroup ubuntu

# Give Ownership of sandbox env to sandbox group and user
RUN chown -R $CODEJAIL_USER:$CODEJAIL_GROUP $CODEJAIL_VENV

WORKDIR /codejail
RUN git clone https://github.com/eduNEXT/codejailservice/ --branch main --depth 1 /codejail
ADD requirements.txt requirements.txt

# Install dependencies
RUN source $CODEJAIL_VENV/bin/activate && pip install --no-cache-dir --no-cache-dir -r requirements/base.txt  && \
    pip install --no-cache-dir -r  https://github.com/openedx/edx-platform/blob/master/requirements/edx-sandbox/py38.txt && deactivate

# Setup sudoers file
ADD sudoers-file/01-sandbox /etc/sudoers.d/01-sandbox

# Change Sudoers file permissions
RUN chmod 0440 /etc/sudoers.d/01-sandbox

# Change Repo ownership
RUN chown -R ubuntu:ubuntu ../codejail

# Switch to ubuntu user
USER ubuntu
CMD gunicorn -b 0.0.0.0:8000 --workers 2 --max-requests=1000 wsgi
