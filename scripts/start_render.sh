#!/usr/bin/env bash
# Render's free tier only offers a single web-service process, no separate
# background-worker service — so this runs the ARQ worker and the FastAPI API
# in the same container. If either one dies, the whole script exits so Render
# restarts the entire service rather than silently running with half the
# pipeline dead.
set -eu

export PYTHONPATH=.

python -m scripts.init_db

arq backend.job_queue.arq_worker.WorkerSettings &
WORKER_PID=$!

uvicorn backend.api.main:app --host 0.0.0.0 --port "${PORT:-8000}" &
API_PID=$!

wait -n "$WORKER_PID" "$API_PID"
kill "$WORKER_PID" "$API_PID" 2>/dev/null || true
exit 1
