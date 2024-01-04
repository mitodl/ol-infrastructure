FROM python:3.11-slim as build
RUN apt-get update -yqq && apt-get install -yqq curl  && useradd -m app
USER app
RUN mkdir /home/app/workspace && chown app:app /home/app/workspace
WORKDIR /home/app/workspace
ENV PATH /bin:/usr/bin/:/usr/local/bin:/home/app/.local/bin
RUN pip install --no-cache-dir poetry
COPY --chown=app:app ./ /home/app/workspace/
RUN scripts/get-pants.sh -d bin &&\
    bin/pants package src/bridge:bridge-package &&\
    bin/pants package src/ol_infrastructure:ol-infrastructure-package &&\
    bin/pants package src/ol_concourse:ol-concourse &&\
    bin/pants package src/ol_concourse/pipelines:ol-concourse-pipelines &&\
    pip install --force-reinstall dist/*.whl &&\
    poetry export --without-hashes -o requirements.txt &&\
    pip install --no-cache-dir -r requirements.txt

FROM python:3.11-slim
RUN useradd -m app
USER app
COPY --from=build /home/app/.local/ /usr/local/
