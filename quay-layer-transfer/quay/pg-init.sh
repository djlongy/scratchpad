#!/bin/sh
# Quay requires the pg_trgm extension (created once by the postgres image's init hook).
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -c 'CREATE EXTENSION IF NOT EXISTS pg_trgm;'
