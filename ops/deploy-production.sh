#!/usr/bin/env bash
set -Eeuo pipefail

deploy_dir="$(pwd -P)"
if [[ "$deploy_dir" != /* || "$deploy_dir" == "/" ]]; then
  echo "Deployment directory must be an absolute, non-root path." >&2
  exit 1
fi
current_env="$deploy_dir/.env.production"
next_env="$deploy_dir/.env.production.next"
previous_env="$deploy_dir/.env.production.previous"
current_compose="$deploy_dir/compose.production.yml"
next_compose="$deploy_dir/compose.production.yml.next"
previous_compose="$deploy_dir/compose.production.yml.previous"
rollback_available=false

if [[ ! -f "$next_env" || ! -f "$next_compose" ]]; then
  echo "Deployment bundle is incomplete." >&2
  exit 1
fi

APP_ENV_FILE="$next_env" docker compose --env-file "$next_env" -f "$next_compose" config --quiet

if [[ -f "$current_env" && -f "$current_compose" ]]; then
  cp -p "$current_env" "$previous_env"
  cp -p "$current_compose" "$previous_compose"
  rollback_available=true
fi

compose() {
  APP_ENV_FILE="$current_env" docker compose --env-file "$current_env" -f "$current_compose" "$@"
}

rollback() {
  code=$?
  trap - ERR
  echo "Deployment failed (exit $code)." >&2
  compose ps >&2 || true
  compose logs --tail=100 backend worker frontend >&2 || true

  if [[ "$rollback_available" == true ]]; then
    echo "Restoring the previous application release." >&2
    cp -p "$previous_env" "$current_env"
    cp -p "$previous_compose" "$current_compose"
    compose up -d --remove-orphans --wait --wait-timeout 180 || true
  else
    echo "No previous release exists; automatic rollback is unavailable." >&2
  fi
  exit "$code"
}
trap rollback ERR

mv -f "$next_env" "$current_env"
mv -f "$next_compose" "$current_compose"
chmod 600 "$current_env"
chmod 644 "$current_compose"

compose pull backend worker frontend postgres redis
compose up -d --wait --wait-timeout 180 postgres redis
compose run --rm backend alembic upgrade head
compose up -d --remove-orphans --wait --wait-timeout 180

health_payload="$(compose exec -T frontend wget -q -O - http://127.0.0.1:8080/api/health)"
grep -Eq '"status"[[:space:]]*:[[:space:]]*"ok"' <<<"$health_payload"

worker_id="$(compose ps -q worker)"
[[ -n "$worker_id" ]]
[[ "$(docker inspect -f '{{.State.Running}}' "$worker_id")" == "true" ]]

release_sha="$(awk -F= '$1 == "RELEASE_SHA" { print substr($0, index($0, "=") + 1) }' "$current_env" | tail -n 1)"
printf '%s\n' "$release_sha" > "$deploy_dir/.deployed-release"

trap - ERR
echo "Production release ${release_sha:-unknown} is healthy."
