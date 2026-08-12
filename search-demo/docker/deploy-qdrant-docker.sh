#!/usr/bin/env bash
#
# Deploy Qdrant via docker-compose.qdrant.yml in this same directory.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"

if ! command -v docker &>/dev/null; then
  echo "Error: docker is not installed or not on PATH." >&2
  exit 1
fi

docker compose -f docker-compose.qdrant.yml up -d

echo
echo "Qdrant is starting up."
echo "  REST API: http://localhost:6333"
echo "  gRPC:     localhost:6334"
echo "  Dashboard: http://localhost:6333/dashboard"
echo
echo "Query service (read-only, for Next.js) is starting up."
echo "  API:      http://localhost:8000/query (POST), /health, /openapi.json"
echo
echo "Logs:   docker compose -f docker-compose.qdrant.yml logs -f"
echo "Stop:   docker compose -f docker-compose.qdrant.yml down"
