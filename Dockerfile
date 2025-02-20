FROM ghcr.io/astral-sh/uv:debian-slim as build
RUN useradd -m app
USER app
RUN mkdir /home/app/workspace && chown app:app /home/app/workspace
WORKDIR /home/app/workspace
ENV PATH /bin:/usr/bin/:/usr/local/bin:/home/app/.local/bin
COPY --chown=app:app ./ /home/app/workspace/
RUN uv build

FROM python:3.13-slim
COPY --from=build /home/app/workspace/dist/*.whl /tmp/
RUN pip install --no-cache /tmp/*.whl
