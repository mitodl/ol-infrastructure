FROM python:3.14-slim@sha256:0741d101873c12ab927e6f8653feb8862b9bd58771177acb1b885b95141f91b4

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
# hadolint ignore=DL3066
USER appuser

CMD ["python", "bot.py"]
