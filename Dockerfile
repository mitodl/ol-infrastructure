FROM ghcr.io/astral-sh/uv:debian-slim as build
ENV DEBIAN_FRONTEND=non-interactive
RUN apt-get update && apt-get install --no-install-recommends -y git && apt-get clean && rm -r /var/lib/apt/lists/* && \
    useradd -m app && mkdir /home/app/workspace && chown app:app /home/app/workspace
USER app
WORKDIR /home/app/workspace
ENV PATH /bin:/usr/bin/:/usr/local/bin:/home/app/.local/bin
COPY --chown=app:app ./ /home/app/workspace/
RUN uv build --all-packages

FROM python:3.14-slim
COPY --from=build /home/app/workspace/dist/*.whl /tmp/
COPY --from=build /home/app/workspace/pyproject.toml /opt/ol-infrastructure/
COPY --from=build /home/app/workspace/uv.lock /opt/ol-infrastructure/
COPY --from=build /home/app/workspace/sdks /opt/ol-infrastructure/sdks
RUN apt-get update && apt-get install --no-install-recommends -y git && apt-get clean && rm -r /var/lib/apt/lists/* && \
    pip install --no-cache-dir /tmp/*.whl
