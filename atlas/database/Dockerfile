# Production database for the Maply atlas: PostgreSQL 17, PostGIS 3.5, pgRouting 3.8.
# Pinned so a redeploy never silently changes the database engine or extensions.
FROM pgrouting/pgrouting:17-3.5-3.8.0

# openssl ships in the base image; it creates the TLS certificate at start.
COPY entrypoint.sh /usr/local/bin/atlas-entrypoint.sh
RUN chmod +x /usr/local/bin/atlas-entrypoint.sh

# Railway mounts the volume at /var/lib/postgresql/data. PGDATA is a
# subdirectory so the volume's own lost+found never blocks initdb.
ENV PGDATA=/var/lib/postgresql/data/pgdata

ENTRYPOINT ["/usr/local/bin/atlas-entrypoint.sh"]
CMD ["postgres"]
