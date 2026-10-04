#!/usr/bin/env bash
set -euo pipefail
image="\$1"
port="\$2"
name="cpa-upgrade-smoke-\$RANDOM"
docker run -d --rm --name "\$name" -p "127.0.0.1:\${port}:\$port" --pull never "\$image" >/dev/null
cleanup() { docker rm -f "\$name" >/dev/null 2>&1 || true; }
trap cleanup EXIT
for _ in \$(seq 1 60); do
  path=health
  [[ "\$port" == 8317 ]] && path=healthz
  if curl -fsS "http://127.0.0.1:\${port}/\${path}" >/dev/null; then exit 0; fi
  sleep 2
done
exit 1
