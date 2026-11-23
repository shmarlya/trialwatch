# syntax=docker/dockerfile:1
# The helper images a deploy needs besides the app's own (Dockerfile at the root), each with its files
# inside, so a host that has only compose.coolify.yaml (Coolify) needs nothing from the repository.
# One target per image; .github/workflows/image.yml builds and pushes each as ghcr.io/shmarlya/trialwatch-<target>.
# Build context: the repository root (ops/images/Dockerfile.dockerignore says what it may read).

# Applies db/migrations/*.sql, then exits (dbmate; its working directory is /, so ./db/migrations)
FROM ghcr.io/amacneil/dbmate:2.37.0 AS migrate
COPY db/migrations /db/migrations
# nowhere to write db/schema.sql to, and two starts at once wait for each other
ENV DBMATE_NO_DUMP_SCHEMA=true DBMATE_MIGRATION_LOCK=true
CMD ["--wait", "up"]

# The front door behind Coolify's proxy: everything to web, /grafana/ to Grafana for admins
FROM caddy:2.10 AS proxy
COPY ops/caddy/Caddyfile.coolify /etc/caddy/Caddyfile

# Grafana with Prometheus, Tempo and Loki, with TrialWatch's settings, data source, dashboards and alerts
FROM grafana/otel-lgtm:0.41.1 AS grafana
COPY ops/lgtm/tempo-config.yaml /otel-lgtm/tempo-config.yaml
COPY ops/lgtm/loki-config.yaml /otel-lgtm/loki-config.yaml
COPY ops/grafana/provisioning/datasources/trialwatch.yaml /otel-lgtm/grafana/conf/provisioning/datasources/trialwatch.yaml
COPY ops/grafana/provisioning/dashboards/trialwatch.yaml /otel-lgtm/grafana/conf/provisioning/dashboards/trialwatch.yaml
COPY ops/grafana/provisioning/alerting/trialwatch.yaml /otel-lgtm/grafana/conf/provisioning/alerting/trialwatch.yaml
COPY ops/grafana/dashboards /otel-lgtm/trialwatch-dashboards

# Postgres's tools with the backup and Grafana-role scripts (ops/db/): compose picks the script to run
FROM postgres:18-alpine AS dbtools
COPY ops/db/backup.sh ops/db/grafana-role.sh /
