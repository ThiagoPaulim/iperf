#!/usr/bin/env sh
set -eu
curl -fsS -X POST "$MININGHUB_URL/api/restore" -H "Authorization: Bearer $MININGHUB_TOKEN" -F "file=@$1"
