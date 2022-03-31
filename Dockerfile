FROM python:3.9-slim as build
RUN apt-get update -yqq && apt-get install -yqq curl  && useradd -m app
USER app
RUN mkdir /home/app/workspace && chown app:app /home/app/workspace
WORKDIR /home/app/workspace
ENV PATH /bin:/usr/bin/:/usr/local/bin:/home/app/.local/bin
RUN pip install --no-cache-dir poetry && poetry config virtualenvs.in-project true
COPY --chown=app:app ./ /home/app/workspace/
RUN poetry install --no-dev &&\
    ./pants package src/bridge:bridge-package &&\
    ./pants package src/ol_infrastructure:ol-infrastructure-package &&\
    .venv/bin/pip install --force-reinstall dist/*.whl

FROM python:3.9-slim
RUN useradd -m app
USER app
WORKDIR /home/app/workspace
ENV PATH /bin:/usr/bin/:/usr/local/bin:/home/app/.local/bin:/home/app/workspace/.venv/bin/pyinfra
COPY --from=build /home/app/workspace/.venv/ .venv
