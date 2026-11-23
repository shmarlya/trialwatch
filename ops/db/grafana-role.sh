#!/bin/sh
# Gives grafana_ro (the role that reads only the report views, db/migrations/*_report.sql) its password
# from GRAFANA_DB_PASSWORD, so Grafana's Postgres data source can sign in; then exits. Run by compose
# after each migration (service grafana-role).
set -eu
if [ -z "${GRAFANA_DB_PASSWORD:-}" ]; then
    echo "GRAFANA_DB_PASSWORD is not set: Grafana's dashboards cannot read the database"
    exit 0
fi
# psql quotes the password itself (:'password'), whatever characters it holds
echo "ALTER ROLE grafana_ro LOGIN PASSWORD :'password';" | psql -v ON_ERROR_STOP=1 -v password="$GRAFANA_DB_PASSWORD"
echo "grafana_ro can sign in"
