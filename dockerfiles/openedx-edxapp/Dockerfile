# Heavily inspired by Tutor from overhang.io

# This file and other files adapted from Tutor are covered by and subject to the AGPL-3
# as a clause 5 'aggregate'.

# This dockerfile is not expected to work locally. Only executes correctly within the context
# of its concourse pipeline.

FROM --platform=linux/amd64 python:3.8.12-buster as base

ENV DEBIAN_FRONTEND=noninteractive
COPY ./apt-pkg-list /root
COPY ./*.pip.txt /root
RUN apt update \
    && apt install -y --no-install-recommends $(cat /root/apt-pkg-list) \
    && apt autoremove -y \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

FROM base as locales
ARG OPENEDX_I18N_VERSION="open-release/olive.3"
RUN cd /tmp \
    && curl -L -o openedx-i18n.tar.gz https://github.com/openedx/openedx-i18n/archive/$OPENEDX_I18N_VERSION.tar.gz \
    && tar xzf /tmp/openedx-i18n.tar.gz \
    && mkdir -p /openedx/locale/contrib \
    && mv openedx-i18n-*/edx-platform/locale /openedx/locale/contrib \
    && rm -rf openedx-i18n*

FROM base as code
# Expected to be run in concourse where the edx-platform code will come from a git resource @ edx_platform
COPY ./edx_platform /openedx/edx-platform

ARG DEPLOYMENT_NAME="invalid"
RUN pip install --user --no-cache-dir -r /root/$DEPLOYMENT_NAME-pkg-list -r /openedx/edx-platform/requirements/edx/base.txt
RUN if [ "$DEPLOYMENT_NAME" = "mitxonline" ]; then pip remove --user --no-cache-dir "edx-name-affirmation"; fi

WORKDIR /openedx/edx-platform
ENV PATH /root/.local/bin:/openedx/nodeenv/bin:${PATH}
ENV NPM_REGISTRY "https://registry.npmjs.org/"
RUN nodeenv /openedx/nodeenv --node=16.14.0 --prebuilt \
    && npm clean-install --verbose --registry=$NPM_REGISTRY

FROM base as themes

# Expected to be run by concourse where the themes will be collected into one directory prior to docker build
COPY ./collected_themes /openedx/themes

FROM base as tutor-utils

ARG TUTOR_REPO="https://github.com/overhangio/tutor.git"
ARG TUTOR_VERSION="v15.3.4"

RUN git clone $TUTOR_REPO --branch $TUTOR_VERSION --depth 1 /openedx/tutor

FROM base as collected

ARG APP_USER_ID=1000
RUN if [ "$APP_USER_ID" = 0 ]; then echo "app user may not be root" && false; fi
RUN useradd --home-dir /openedx --create-home --shell /bin/bash/ --uid ${APP_USER_ID} app
USER ${APP_USER_ID}

# digest for docker.io/powerman/dockerize:0.19.0 --platform=linux/amd64
COPY --from=docker.io/powerman/dockerize@sha256:12b06425c45a6d7463503262c570d5dbb81cfe86ae70dd1863c85494b1bdf910 /usr/local/bin/dockerize /usr/local/bin/dockerize
COPY --chown=app:app --from=locales /openedx/locale /openedx/locale
COPY --chown=app:app --from=code /openedx/edx-platform /openedx/edx-platform
COPY --chown=app:app --from=code /openedx/nodeenv /openedx/nodeenv
COPY --chown=app:app --from=code /root/.local /openedx/.local
COPY --chown=app:app --from=themes /openedx/themes /openedx/themes
COPY --chown=app:app --from=tutor-utils /openedx/tutor/tutor/templates/build/openedx/bin /openedx/bin
RUN chmod a+x /openedx/bin/*

ENV PATH /openedx/.local/bin:/openedx/bin:/openedx/edx-platform/node_modules/.bin:/openedx/nodeenv/bin:${PATH}
WORKDIR /openedx/edx-platform

RUN pip install --user --no-cache-dir -e .

# TODO MD 20230522 Not sure if we need to be making this mitol directory
RUN mkdir -p /openedx/config  ./lms/envs/mitol ./cms/envs/mitol

# TODO MD 20230522 Not sure what revisions.yml is for
COPY --chown=app:app settings/revisions.yml /openedx/config/revisions.yml

# These are dummy configs to allow some of the collectstatic steps to run
COPY --chown=app:app settings/lms.env.yml /openedx/config/lms.env.yml
COPY --chown=app:app settings/cms.env.yml /openedx/config/cms.env.yml

# TODO MD 20230522 What is going on with these? Not sure if they are ever used
COPY --chown=app:app settings/lms/assets.not_py /openedx/edx-platform/lms/envs/mitol/assets.py
COPY --chown=app:app settings/lms/i18n.not_py /openedx/edx-platform/lms/envs/mitol/i18n.py
COPY --chown=app:app settings/cms/assets.not_py /openedx/edx-platform/cms/envs/mitol/assets.py
COPY --chown=app:app settings/cms/i18n.not_py /openedx/edx-platform/cms/envs/mitol/i18n.py

# Copy in special waffle flag setting script
COPY --chown=app:app settings/set_waffle_flags.not_py /openedx/edx-platform/set_waffle_flags.py

FROM collected AS build-static-assets

ENV REVISION_CFG /openedx/config/revisions.yml
ENV LMS_CFG /openedx/config/lms.env.yml
ENV CMS_CFG /openedx/config/cms.env.yml

ENV DJANGO_SETTINGS_MODULE lms.envs.production
ENV NO_PYTHON_UNINSTALL 1
ENV NO_PREREQ_INSTALL 0

# These are important but their provanance is unknown at this time.
# The reason they need to be installed like this is also unknown.
RUN pip install --user --no-cache-dir -e "git+https://github.com/openedx/codejail.git@babbe784b48bb9888aa159d8b401cbe5e07f0af4#egg=codejail" \
    && pip install --user --no-cache-dir -e "git+https://github.com/openedx/django-wiki.git@0a1d555a1fa2834cc46367968aad907a5667317b#egg=django_wiki" \
    && pip install --user --no-cache-dir -e "git+https://github.com/openedx/blockstore.git@9d623bc04f479a1af29204d438fa442ea8380fae#egg=blockstore" \
    && pip install --user --no-cache-dir -e "git+https://github.com/openedx/olxcleaner.git@2f0d6c7f126cbd69c9724b7b57a0b2565330a297#egg=olxcleaner"

RUN openedx-assets xmodule \
    && openedx-assets npm \
    && openedx-assets webpack --env=prod \
    && openedx-assets common

RUN mkdir -p /openedx/data/var/edxapp/course_repos \
    && mkdir -p /openedx/data/var/log/edx

RUN paver compile_sass --theme-dirs /openedx/themes/ \
    && openedx-assets collect --settings=mitol.assets \
    && rdfind -makesymlinks true -followsymlinks true /openedx/staticfiles/

FROM build-static-assets AS final

ENV UWSGI_WORKERS=2
# DJANGO_SETTINGS_MODULE MUST be provided at runtime to control if we're a CMS or LMS
ENV DJANGO_SETTINGS_MODULE="invalid"

# Setup known_hosts file for git-import-export-plugin
RUN mkdir /openedx/.ssh \
    && chown app:app /openedx/.ssh \
    && chmod 0700 /openedx/.ssh \
    && ssh-keyscan 'github.com' 'github.mit.edu' >> /openedx/.ssh/known_hosts \
    && chmod 0600 /openedx/.ssh/known_hosts

CMD uwsgi uwsgi.ini
