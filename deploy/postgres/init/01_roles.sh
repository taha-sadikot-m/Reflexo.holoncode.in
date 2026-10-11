#!/bin/bash
# Creates application roles and the Temporal role on first container start (docker-entrypoint-initdb.d).
# The owner role is POSTGRES_USER (reflexo_owner); it owns all objects and is used only by migrations.
set -euo pipefail

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE ROLE reflexo_app LOGIN PASSWORD '${REFLEXO_APP_DB_PASSWORD:-app_dev}' NOSUPERUSER NOBYPASSRLS;
  CREATE ROLE reflexo_service LOGIN PASSWORD '${REFLEXO_SERVICE_DB_PASSWORD:-service_dev}' NOSUPERUSER NOBYPASSRLS;
  GRANT CONNECT ON DATABASE ${POSTGRES_DB} TO reflexo_app, reflexo_service;
  CREATE ROLE temporal LOGIN PASSWORD '${TEMPORAL_DB_PASSWORD:-temporal_dev}' CREATEDB;
EOSQL

# Temporal databases (auto-setup creates the schema, but expects the databases to exist)
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
  CREATE DATABASE temporal OWNER temporal;
  CREATE DATABASE temporal_visibility OWNER temporal;
EOSQL
