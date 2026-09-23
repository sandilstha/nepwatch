#!/usr/bin/env bash
# Container entrypoint: wait for MySQL to accept connections, apply migrations,
# gather static files, then hand off to the CMD (gunicorn by default).
set -e

DB_HOST="${DB_HOST:-db}"
DB_PORT="${DB_PORT:-3306}"

echo "[entrypoint] waiting for database at ${DB_HOST}:${DB_PORT} ..."
python - <<'PY'
import os, socket, sys, time

host = os.environ.get("DB_HOST", "db")
port = int(os.environ.get("DB_PORT", "3306"))
for attempt in range(60):
    try:
        with socket.create_connection((host, port), timeout=2):
            print(f"[entrypoint] database is up (after {attempt * 2}s)")
            break
    except OSError:
        time.sleep(2)
else:
    print("[entrypoint] ERROR: database never became reachable", file=sys.stderr)
    sys.exit(1)
PY

echo "[entrypoint] applying database migrations ..."
python manage.py migrate --noinput

echo "[entrypoint] collecting static files ..."
python manage.py collectstatic --noinput

echo "[entrypoint] starting: $*"
exec "$@"
