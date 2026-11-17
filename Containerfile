FROM docker.io/library/debian:12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        isync \
        ca-certificates \
        python3-flask \
        gunicorn \
        pst-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY *.py VERSION ./
COPY templates/ templates/
COPY static/ static/

RUN mkdir -p /data

# The secret key (session signing, stored-credential encryption) is made on
# first start and kept in the data volume. It is never part of the image:
# every container started from one image would share it (1.3.3).
ENV MAIL_ARCHIVER_DATA=/data \
    MAIL_ARCHIVER_SECRET_FILE=/data/.secret_key \
    MAIL_ARCHIVER_AUTH=builtin

EXPOSE 8400
VOLUME /data

CMD ["sh", "-c", "umask 077; [ -s \"$MAIL_ARCHIVER_SECRET_FILE\" ] || python3 -c 'import secrets; print(secrets.token_hex(32))' > \"$MAIL_ARCHIVER_SECRET_FILE\"; exec gunicorn --bind 0.0.0.0:8400 --workers 2 --timeout 120 app:app"]
