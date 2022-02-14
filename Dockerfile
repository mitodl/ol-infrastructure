FROM python:3.9-slim
RUN useradd -m app
USER app
WORKDIR /home/app
RUN pip install --no-cache-dir poetry
ENV PATH /bin:/usr/bin/:/usr/local/bin:/home/app/.local/bin
RUN poetry config virtualenvs.in-project true
COPY ./pyproject.toml pyproject.toml
COPY ./poetry.lock poetry.lock
COPY --chown=app ./src src
COPY ./README.md README.md
RUN poetry install --no-dev
RUN rm -rf src/
