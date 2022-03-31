FROM python:3.9-slim as build
RUN apt-get update -yqq && apt-get install -yqq curl  && useradd -m app
USER app
RUN mkdir /home/app/workspace && chown app:app /home/app/workspace
WORKDIR /home/app/workspace
ENV PATH /bin:/usr/bin/:/usr/local/bin:/home/app/.local/bin
RUN pip install --no-cache-dir poetry
COPY --chown=app:app ./ /home/app/workspace/
RUN poetry export --without-hashes -o requirements.txt &&\
    ./pants package src/bridge:bridge-package &&\
    ./pants package src/ol_infrastructure:ol-infrastructure-package &&\
    pip install --force-reinstall dist/*.whl &&\
    pip install --no-cache-dir -r requirements.txt

FROM python:3.9-slim
RUN useradd -m app
USER app
COPY --from=build /home/app/.local/ /usr/local/
