ARG DOCKER_IMAGE_TAG=3.4-slim-bullseye
FROM docker.io/ruby:$DOCKER_IMAGE_TAG
ENV DEBIAN_FRONTEND=noninteractive
RUN apt update && \
  apt upgrade -y && \
  apt install -y git wget autoconf bison build-essential libssl-dev libyaml-dev libreadline6-dev zlib1g-dev libncurses5-dev libffi-dev libgdbm-dev libxml2-dev libxslt-dev

# Install dockerize to wait for mongodb/elasticsearch availability
ARG DOCKERIZE_VERSION=v0.6.1
ARG OPENEDX_COMMON_VERSION=open-release/quince.master
ARG OPENEDX_FORUM_REPOSITORY=https://github.com/openedx/cs_comments_service

RUN wget -O /tmp/dockerize.tar.gz https://github.com/jwilder/dockerize/releases/download/$DOCKERIZE_VERSION/dockerize-linux-amd64-$DOCKERIZE_VERSION.tar.gz \
    && tar -C /usr/local/bin -xzvf /tmp/dockerize.tar.gz \
    && rm /tmp/dockerize.tar.gz

# Create unprivileged "app" user
RUN useradd --home-dir /app --create-home --shell /bin/bash --uid 1000 app

# Copy custom scripts
COPY ./bin /app/bin
RUN chmod a+x /app/bin/* && mkdir -p /opt/forum && chmod a+x /opt/forum && chown app:app /opt/forum
ENV PATH :${PATH}

# From then on, run as unprivileged app user
USER app

# Install rake and bundler
ENV PATH "/app/bin:/app/.gem/ruby/3.3.0/bin:/app/.local/share/gem/ruby/3.3.0/bin:$PATH"
RUN gem install --user-install bundler \
    && gem install --user-install rake

# Install forum
ARG GEMFILE_FILE=Gemfile3
ENV BUNDLE_GEMFILE ${GEMFILE_FILE}
RUN git clone $OPENEDX_FORUM_REPOSITORY --branch $OPENEDX_COMMON_VERSION --depth 1 /app/cs_comments_service
WORKDIR /app/cs_comments_service
RUN bundle config set deployment true && bundle install

ENTRYPOINT ["/app/bin/docker-entrypoint.sh"]

ENV SINATRA_ENV staging
ENV NEW_RELIC_ENABLE false
ENV API_KEY forumapikey
ENV SEARCH_SERVER "http://elasticsearch:9200"
ENV MONGODB_AUTH ""
ENV MONGOID_AUTH_MECH ":scram"
ENV MONGODB_PORT "27017"
ENV MONGODB_DATABASE "forum"
EXPOSE 4567
CMD ./bin/unicorn -c config/unicorn_tcp.rb -I '.'
