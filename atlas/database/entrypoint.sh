#!/bin/bash
# Enables TLS on the public TCP proxy with a self-signed certificate kept on the
# volume, then hands over to the stock postgres entrypoint unchanged. WAL is capped
# so publishes fit Railway's 500 MB free-plan volume (raise PG_MAX_WAL_SIZE later).
set -euo pipefail

cert_dir=/var/lib/postgresql/data/certs
mkdir -p "$cert_dir"
if [ ! -s "$cert_dir/server.crt" ] || ! openssl x509 -checkend 2592000 -noout -in "$cert_dir/server.crt" >/dev/null; then
  openssl req -new -x509 -nodes -days "${SSL_CERT_DAYS:-3650}" -subj "/CN=maply-atlas" \
    -keyout "$cert_dir/server.key" -out "$cert_dir/server.crt" >/dev/null 2>&1
fi
chown -R postgres:postgres "$cert_dir"
chmod 600 "$cert_dir/server.key"

if [ "${1:-}" = "postgres" ]; then
  shift
  set -- postgres \
    -c ssl=on \
    -c ssl_cert_file="$cert_dir/server.crt" \
    -c ssl_key_file="$cert_dir/server.key" \
    -c listen_addresses='*' \
    -c max_wal_size="${PG_MAX_WAL_SIZE:-128MB}" \
    -c min_wal_size=32MB \
    "$@"
fi
exec docker-entrypoint.sh "$@"
