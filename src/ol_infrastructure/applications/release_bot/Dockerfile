FROM python:3.14-slim@sha256:51dafde81dbdb6ebde285137a295cf18a47ca95234fe388a343719cb97305b3d

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY *.py .

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
# hadolint ignore=DL3066
USER appuser

CMD ["python", "bot.py"]
