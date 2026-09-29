FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# SQLite + photos live on a mounted volume. Before the COPY, so a code change
# reuses this layer instead of paying a container start for a mkdir.
RUN mkdir -p /app/data/photos

# Baked in so the image runs on its own; compose mounts app/ over it
# and a code deploy is a restart, not a rebuild (docker-compose.yml).
COPY app/ ./app/
ENV DATABASE_PATH=/app/data/steelshelf.db \
    PHOTO_DIR=/app/data/photos

EXPOSE 8010

# Runs as root on purpose: /app/data is a bind mount owned by whichever host
# user made it, and a fixed container uid would not match it.

# Probes /healthz, which runs a count over `items` and answers 503 if the
# bind mount is gone — `/` never touches the database, so it would not.
HEALTHCHECK --interval=60s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/healthz', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8010"]
