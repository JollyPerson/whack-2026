#!/usr/bin/env bash
# Runs on the server: deploy.sh IMAGE, with an optional registry token on stdin.
# Starts IMAGE and waits for its health check. If it never gets healthy, goes back to the previous image and fails.
set -euo pipefail
cd "$(dirname "$0")"

new=${1:?usage: deploy.sh IMAGE}
registry=${new%%/*}
compose() { docker compose --env-file .env --env-file image.env "$@"; }
start() { printf 'IMAGE=%s\n' "$1" > image.env; compose up -d --wait --wait-timeout 90 --remove-orphans; }

prev=""
if [ -f image.env ]; then prev=$(sed -n 's/^IMAGE=//p' image.env); fi

token=""
if [ ! -t 0 ]; then token=$(cat); fi
if [ -n "$token" ]; then
  trap 'docker logout "$registry" >/dev/null 2>&1 || true' EXIT
  printf '%s' "$token" | docker login "$registry" -u github-actions --password-stdin >/dev/null
fi
docker pull -q "$new"

if ! start "$new"; then
  echo "ERROR: $new did not become healthy. Its logs:" >&2
  compose logs --tail 50 app >&2 || true
  if [ -n "$prev" ]; then
    echo "Rolling back to $prev" >&2
    start "$prev" || echo "ERROR: rollback to $prev also failed, the site is down" >&2
  else
    echo "No previous image to roll back to." >&2
  fi
  exit 1
fi

# `up` leaves caddy running when only the Caddyfile changed.
compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile \
  || echo "WARN: caddy reload failed, the previous Caddyfile is still in use" >&2
docker image prune -af --filter until=168h >/dev/null
printf '%s %s\n' "$(date -u +%FT%TZ)" "$new" >> deploys.log
echo "Deployed $new"
