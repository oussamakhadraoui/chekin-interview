#!/bin/sh
set -e

# Migrations run on boot so `docker compose up` is the only command a reviewer needs.
# In a real deployment this would be a separate job in the release pipeline, not the
# app entrypoint -- N instances booting at once would all race to migrate. Alembic
# takes a lock so that is safe, but it couples app rollout to schema rollout.
echo "running migrations..."
alembic upgrade head

echo "starting api..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
