#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

deploy_dir="${1:-/opt/trip-planner}"
backup_dir="${2:-/var/backups/trip-planner}"

if [[ "$deploy_dir" != /* || "$deploy_dir" == "/" ]]; then
  echo "Deployment directory must be an absolute, non-root path." >&2
  exit 1
fi
deploy_dir="$(realpath "$deploy_dir")"
if [[ "$deploy_dir" == "/" ]]; then
  echo "Resolved deployment directory must not be root." >&2
  exit 1
fi
if [[ "$backup_dir" != /* || "$backup_dir" == "/" ]]; then
  echo "Backup directory must be an absolute, non-root path." >&2
  exit 1
fi

env_file="$deploy_dir/.env.production"
compose_file="$deploy_dir/compose.production.yml"
[[ -f "$env_file" && -f "$compose_file" ]]

install -d -m 700 "$backup_dir"
backup_dir="$(realpath "$backup_dir")"
if [[ "$backup_dir" == "/" ]]; then
  echo "Resolved backup directory must not be root." >&2
  exit 1
fi
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
target="$backup_dir/postgres-${timestamp}.dump"
[[ ! -e "$target" ]]
temporary="$(mktemp "$backup_dir/.postgres-${timestamp}.XXXXXX")"

cleanup() {
  rm -f -- "$temporary"
}
trap cleanup EXIT

docker compose --env-file "$env_file" -f "$compose_file" exec -T postgres \
  sh -ec 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$temporary"

[[ -s "$temporary" ]]
mv -- "$temporary" "$target"
trap - EXIT

if command -v sha256sum >/dev/null 2>&1; then
  sha256sum "$target" > "$target.sha256"
fi

echo "$target"
