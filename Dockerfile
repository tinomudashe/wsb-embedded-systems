# syntax=docker/dockerfile:1

# ---- 1. Build the dashboard -------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
# vite.config.js writes to ../server/static (i.e. /server/static here).
RUN npm run build

# ---- 2. Runtime: FastAPI + built dashboard ----------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STATIC_DIR=/app/static

WORKDIR /app

# Non-root runtime user; /var/lib/wsb is the persistent SQLite/data mount.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin wsb \
    && mkdir -p /var/lib/wsb \
    && chown wsb:wsb /var/lib/wsb

COPY server/requirements.txt ./
RUN pip install -r requirements.txt

COPY server/app ./app
COPY --from=web /server/static ./static
RUN chown -R wsb:wsb /app

USER wsb

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
