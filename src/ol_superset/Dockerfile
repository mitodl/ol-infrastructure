ARG SUPERSET_TAG=6.0.0

# Stage 1: build the NL Explorer frontend
FROM node:20-slim AS nl-explorer-frontend
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git && rm -rf /var/lib/apt/lists/*
RUN git clone --depth=1 https://github.com/mitodl/superset-nl-explorer.git /build/nl-explorer
WORKDIR /build/nl-explorer/frontend
RUN npm ci && npm run build

FROM apache/superset:${SUPERSET_TAG}
COPY --link requirements.txt /tmp/requirements.txt
COPY static/assets/images/ol-data-platform-logo.svg /app/superset/static/assets/images/ol-data-platform-logo.svg
# Copy extension manifest + compiled frontend assets into the image
COPY --from=nl-explorer-frontend /build/nl-explorer/extension.json /app/extensions/nl-explorer/extension.json
COPY --from=nl-explorer-frontend /build/nl-explorer/dist/ /app/extensions/nl-explorer/dist/
USER root
WORKDIR /app
# hadolint ignore=DL3008
RUN chmod 1777 /tmp && apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*
RUN uv pip install --no-cache-dir -r /tmp/requirements.txt
USER superset
