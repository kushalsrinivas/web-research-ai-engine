#!/usr/bin/env bash
# End-to-end smoke check for the local/staging compose stack.
set -euo pipefail

BASE="${BASE:-http://127.0.0.1}"
SERVICES="api:8080 scheduler:8081 fetcher:8082 indexer:8083 reconciler:8084 parser:8085 enricher:8086 graph-builder:8087 llm-gateway:8088 research-orchestrator:8089"

wait_http() {
  local name="$1" url="$2" attempts="${3:-60}" i=0
  while [ "$i" -lt "$attempts" ]; do
    if curl -fsS --max-time 2 "$url" >/dev/null 2>&1; then
      echo "ok: $name"
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  echo "FAIL: $name ($url)" >&2
  return 1
}

wait_loki_trace() {
  local trace="$1" attempts="${2:-60}" i=0
  local query="{container=~\"wre-.*\"} |= \"$trace\""
  local start_ns=$((($(date +%s) - 600) * 1000000000))
  local end_ns=$(($(date +%s) * 1000000000))
  while [ "$i" -lt "$attempts" ]; do
    if curl -fsS --max-time 3 -G "$BASE:3100/loki/api/v1/query_range" \
      --data-urlencode "query=$query" \
      --data-urlencode "start=$start_ns" \
      --data-urlencode "end=$end_ns" \
      --data-urlencode "limit=10" 2>/dev/null | grep -q "$trace"; then
      echo "ok: loki trace-correlated log"
      return 0
    fi
    i=$((i + 1))
    sleep 1
  done
  echo "FAIL: loki trace-correlated log ($trace)" >&2
  return 1
}

for entry in $SERVICES; do
  name="${entry%%:*}"
  port="${entry##*:}"
  wait_http "$name /healthz" "$BASE:$port/healthz"
  wait_http "$name /readyz" "$BASE:$port/readyz"
done

wait_http "prometheus" "$BASE:9090/-/healthy"
wait_http "grafana" "$BASE:3000/api/health"
wait_http "loki" "$BASE:3100/ready"
wait_http "tempo" "$BASE:3200/ready"
wait_http "object store" "$BASE:9000/minio/health/live"

redpanda_ready=0
i=0
while [ "$i" -lt 60 ]; do
  if docker compose exec -T redpanda rpk cluster health 2>/dev/null | grep -q "Healthy:.*true"; then
    redpanda_ready=1
    break
  fi
  i=$((i + 1))
  sleep 1
done
if [ "$redpanda_ready" -ne 1 ]; then
  echo "FAIL: redpanda" >&2
  exit 1
fi
echo "ok: redpanda"

migrations=$(docker compose exec -T postgres \
  psql -U "${POSTGRES_USER:-wre}" -d "${POSTGRES_DB:-wre}" -Atq \
  -c "select count(*) from schema_migrations")
if [ "${migrations:-0}" -lt 2 ]; then
  echo "FAIL: expected at least 2 applied migrations, got $migrations" >&2
  exit 1
fi
echo "ok: migrations ($migrations)"

core_tables=$(docker compose exec -T postgres \
  psql -U "${POSTGRES_USER:-wre}" -d "${POSTGRES_DB:-wre}" -Atq \
  -c "select count(*) from information_schema.tables where table_schema = 'core'")
if [ "${core_tables:-0}" -lt 5 ]; then
  echo "FAIL: expected the core schema, got $core_tables tables" >&2
  exit 1
fi
echo "ok: core schema ($core_tables tables)"

# A single trace must cross the Go API and Python parser, be visible in Tempo,
# and have its trace id in Loki logs.
api_trace=$(uuidgen | tr -d '-' | tr '[:upper:]' '[:lower:]')
parser_trace=$(uuidgen | tr -d '-' | tr '[:upper:]' '[:lower:]')
curl -fsS -H "traceparent: 00-${api_trace}-00f067aa0ba902b7-01" "$BASE:8080/healthz" >/dev/null
curl -fsS -H "traceparent: 00-${parser_trace}-00f067aa0ba902b8-01" "$BASE:8085/healthz" >/dev/null

wait_http "tempo api trace" "$BASE:3200/api/traces/$api_trace"
wait_http "tempo parser trace" "$BASE:3200/api/traces/$parser_trace"
wait_loki_trace "$api_trace"

echo "smoke: ok"
