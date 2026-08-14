#!/usr/bin/env bash
#
# run_hops_scale_async.sh
# Async (FastAPI/httpx) counterpart of run_hops_scale.sh: same full-corpus
# (k=4096, 98k courses) virtual-node hops-vs-nprobe sweep, on the async_app_vnodes.py
# launcher / docker-compose.async_vnodes.yml ring instead of the xmlrpc one.
#
# Usage:  ./run_hops_scale_async.sh [VNODES_PER_CONTAINER] [BOOT_SEC]
#   ./run_hops_scale_async.sh 10     -> 5x10 = 50-node ring  (recommended first)
#   ./run_hops_scale_async.sh 20     -> 5x20 = 100-node ring (matches the sync headline run)
#   defaults: 10  30
#
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CE="$ROOT/containerized_environment"
COMPOSE="$CE/docker-compose.async_vnodes.yml"
EVAL="src.benchmarks.containerized.evaluate"

VNODES="${1:-10}"
BOOT="${2:-30}"
DS=98104
QFILE="data/benchmarks/queries/queries_98k_k4096.json"
CONTAINERS="av-bootstrap,av-node-1,av-node-2,av-node-3,av-node-4"
CONVERGE_TIMEOUT=900

cd "$CE"

run_arch () {  # arch
  local arch="$1"
  echo
  echo "=================================================================="
  echo ">>> [$arch] $((VNODES*5))-node ring | k=4096 | full corpus ($DS)"
  echo "=================================================================="
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" up -d --build
  echo ">>> booting ($BOOT s), then the benchmark's own convergence gates take over..."
  sleep "$BOOT"
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" run --rm runner \
    -m "$EVAL" --arch "$arch" --mode hops \
    --containers "$CONTAINERS" --vnodes_per_container "$VNODES" \
    --dataset_size "$DS" --queries_file "$QFILE" \
    --converge_timeout "$CONVERGE_TIMEOUT"
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
}

run_arch async_semantic
run_arch async_clustered

echo "=== ASYNC SCALE RUN DONE ($((VNODES*5)) nodes) ==="
echo "    results : data/benchmarks/results/containerized/hops_sweep_results_async_*.json"
