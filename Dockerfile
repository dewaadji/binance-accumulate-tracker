FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
  && apt-get install -y --no-install-recommends ca-certificates curl \
  && rm -rf /var/lib/apt/lists/*

ARG SUPERCRONIC_VERSION=v0.2.30
ARG TARGETARCH
RUN case "$TARGETARCH" in \
      amd64) ARCH="amd64" ;; \
      arm64) ARCH="arm64" ;; \
      *) echo "Unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
  && curl -fsSLo /usr/local/bin/supercronic "https://github.com/aptible/supercronic/releases/download/${SUPERCRONIC_VERSION}/supercronic-linux-${ARCH}" \
  && chmod +x /usr/local/bin/supercronic

RUN pip install --no-cache-dir requests

RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin app \
  && mkdir -p /data/logs \
  && chown -R app:app /data

COPY --chown=app:app accumulation_radar.py /app/accumulation_radar.py
COPY --chown=app:app crontab /etc/supercronic/crontab
COPY --chown=app:app docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER app

ENV DB_PATH=/data/accumulation.db

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
