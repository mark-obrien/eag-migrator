FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

# default-mysql-client / postgresql-client let you load dumps and poke at the
# databases from inside the migrator container without extra tooling.
RUN apt-get update && apt-get install -y --no-install-recommends \
        default-mysql-client \
        postgresql-client \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-deps -e .

# `eagm capture` and `eagm login --form` drive a real browser, and reading v2
# over HTTP is the primary path here, so Chromium is included by default.
# It adds several hundred MB; if you only ever migrate database-to-database,
# build with --build-arg WITH_BROWSER=false (or `make build-slim`).
ARG WITH_BROWSER=true
RUN if [ "$WITH_BROWSER" = "true" ]; then \
        playwright install --with-deps chromium; \
    fi

# Mounted as volumes by docker-compose so output survives the container.
RUN mkdir -p /app/profiles /app/reports /app/state /app/config

ENTRYPOINT ["eagm"]
CMD ["--help"]
