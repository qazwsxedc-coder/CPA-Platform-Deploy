#!/usr/bin/env bash
set -euo pipefail
image="\$1"
port="\$2"
name="cpa-upgrade-smoke-\$RANDOM"
tmp="$(mktemp -d)"
if [[ "\$port" == 18317 ]]; then
  mkdir -p "\$tmp/data" "\$tmp/secret"
  printf 'cpa-smoke-admin-key' > "\$tmp/secret/admin-key"
  docker run -d --rm --name "\$name" -p "127.0.0.1:\${port}:\$port" --pull never \
    -e HTTP_ADDR=0.0.0.0:18317 -e USAGE_DATA_DIR=/data -e USAGE_DB_PATH=/data/usage.sqlite \
    -e CPA_MANAGER_DATA_KEY_PATH=/data/data.key -e CPA_MANAGER_ADMIN_KEY_FILE=/run/secrets/admin-key \
    -e CPAMP_UPDATE_CHECK_ENABLED=false -v "\$tmp/data:/data" -v "\$tmp/secret:/run/secrets:ro" "\$image" >/dev/null
else
  docker run -d --rm --name "\$name" -p "127.0.0.1:\${port}:\$port" --pull never "\$image" >/dev/null
fi
cleanup() { docker rm -f "\$name" >/dev/null 2>&1 || true; }
trap 'cleanup; rm -rf "\$tmp"' EXIT
for _ in \$(seq 1 60); do
  path=health
  [[ "\$port" == 8317 ]] && path=healthz
  if curl -fsS "http://127.0.0.1:\${port}/\${path}" >/dev/null; then exit 0; fi
  sleep 2
done
exit 1
