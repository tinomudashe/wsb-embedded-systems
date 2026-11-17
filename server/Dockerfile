# syntax=docker/dockerfile:1
# Build context: server/   (docker compose uses `context: ./server`)
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Non-root runtime user; /var/lib/wsb is the persistent data mount.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin wsb \
    && mkdir -p /var/lib/wsb \
    && chown wsb:wsb /var/lib/wsb

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app
COPY static ./static
RUN chown -R wsb:wsb /app

USER wsb

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
