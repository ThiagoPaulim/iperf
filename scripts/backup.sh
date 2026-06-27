#!/usr/bin/env sh
set -eu
curl -fsS -X POST "$MININGHUB_URL/api/backups" -H "Authorization: Bearer $MININGHUB_TOKEN"
