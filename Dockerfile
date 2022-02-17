FROM python:3.9-slim as build
RUN apt-get update && apt-get install -q -y curl
RUN useradd -m app
USER app
WORKDIR /home/app/workspace
ENV PATH /bin:/usr/bin/:/usr/local/bin:/home/app/.local/bin
RUN pip install --no-cache-dir poetry && poetry config virtualenvs.in-project true
COPY ./ /home/app/workspace/
RUN poetry install --no-dev &&\
    ./pants package src/bridge:bridge-package &&\
    ./pants package src/ol_infrastructure:ol-infrastructure-package &&\
    .venv/bin/pip install dist/*.whl

FROM python:3.9-slim
RUN useradd -m app
USER app
WORKDIR /home/app
COPY --from=build /home/app/workspace/.venv/ .venv
