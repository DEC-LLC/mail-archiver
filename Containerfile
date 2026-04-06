FROM docker.io/library/debian:12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        isync \
        ca-certificates \
        python3-flask \
        gunicorn \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY app.py search_index.py oauth2_microsoft.py ./
COPY templates/ templates/
COPY static/ static/

RUN mkdir -p /data && \
    python3 -c "import secrets; print(secrets.token_hex(32))" > /app/.secret_key && \
    chmod 600 /app/.secret_key

ENV MAIL_ARCHIVER_DATA=/data \
    MAIL_ARCHIVER_SECRET_FILE=/app/.secret_key \
    MAIL_ARCHIVER_AUTH=builtin

EXPOSE 8400
VOLUME /data

CMD ["gunicorn", "--bind", "0.0.0.0:8400", "--workers", "2", "--timeout", "120", "app:app"]
