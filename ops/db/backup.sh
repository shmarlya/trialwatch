#!/bin/sh
# Backs the database up: a pg_dump (custom format) into /backups (./data/backups on the host) at start and
# then every BACKUP_EVERY_SECONDS (a day), keeping BACKUP_KEEP_DAYS days of them. Each one is recorded in
# core.backup: Grafana alerts when the newest is older than 26 hours. Copying /backups off the machine is
# the host's job (docs/operations.md). Run by compose (service backup).
set -u
while true; do
    file="trialwatch-$(date -u +%Y-%m-%dT%H%MZ).dump"
    if pg_dump -Fc -f "/backups/$file.part" && mv "/backups/$file.part" "/backups/$file"; then
        bytes=$(stat -c %s "/backups/$file")
        echo "INSERT INTO core.backup (file, bytes) VALUES (:'file', :bytes);" |
            psql -q -v ON_ERROR_STOP=1 -v file="$file" -v bytes="$bytes" &&
            echo "backed up to $file ($bytes bytes)"
    else
        echo "backup failed" >&2
        rm -f "/backups/$file.part"
    fi
    find /backups -name 'trialwatch-*.dump' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
    sleep "${BACKUP_EVERY_SECONDS:-86400}"
done
