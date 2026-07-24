#!/usr/bin/env bash
#
# run_vnode_disaster.sh
# Disaster-scenario counterpart of run_hops_scale.sh: same large virtual-node
# ring (5 containers x VNODES_PER_CONTAINER vnodes each), but tests whether
# semantic's disaster-recall disadvantage (a contiguous topic region wiped
# out) narrows at finer ring granularity -- unlike run_hops_sweep, this KEEPS
# replication enabled (RF=2, config.vnodes.yaml default) since the whole
# mechanic depends on replicas existing to be wiped out.
#
# Uses the ASYNC vnode stack (docker-compose.async_vnodes.yml / av-* containers,
# FastAPI/httpx transport) for consistency with the rest of the async-only
# reporting scope, not the older sync/xmlrpc docker-compose.vnodes.yml.
#
# Usage:  ./run_vnode_disaster.sh [VNODES_PER_CONTAINER] [BOOT_SEC]
#   ./run_vnode_disaster.sh 20 30   -> 5x20 = 100-node ring (matches the headline hops run)
#   defaults: 20  30
#
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CE="$ROOT/containerized_environment"
COMPOSE="$CE/docker-compose.async_vnodes.yml"
EVAL="src.benchmarks.containerized.evaluate"

VNODES="${1:-20}"
BOOT="${2:-30}"
DS="${DS:-98104}"
# Uniform query set (500 queries, each tagged with its cluster). Override QFILE
# for the krylov large run; NPROBES stays modest so 500 queries stay cheap and
# clear of the high-nprobe timeout cliff. DISASTER_CLUSTER=-1 => heaviest node.
QFILE="${QFILE:-data/benchmarks/queries/queries_98k_uniform_500.json}"
NPROBES="${NPROBES:-1,5,20}"
DISASTER_CLUSTER="${DISASTER_CLUSTER:--1}"
ARCHES="${ARCHES:-async_semantic,async_clustered}"
CONTAINERS="av-bootstrap,av-node-1,av-node-2,av-node-3,av-node-4"
CONVERGE_TIMEOUT=900

cd "$CE"

run_arch () {  # arch
  local arch="$1"
  echo
  echo "=================================================================="
  echo ">>> [$arch] $((VNODES*5))-node ring | disaster scenario | k=5500 | full corpus ($DS)"
  echo "=================================================================="
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" up -d --build
  echo ">>> booting ($BOOT s), then the benchmark's own convergence gates take over..."
  echo ">>> queries: $QFILE | nprobe: $NPROBES | epicenter cluster: $DISASTER_CLUSTER"
  sleep "$BOOT"
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" run --rm runner \
    -m "$EVAL" --arch "$arch" --mode vnode_disaster \
    --containers "$CONTAINERS" --vnodes_per_container "$VNODES" \
    --dataset_size "$DS" --queries_file "$QFILE" \
    --nprobe_list "$NPROBES" --disaster_cluster "$DISASTER_CLUSTER" \
    --converge_timeout "$CONVERGE_TIMEOUT"
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
}

IFS=',' read -ra _archs <<< "$ARCHES"
for _a in "${_archs[@]}"; do run_arch "$_a"; done

echo
echo "=== VNODE DISASTER RUN DONE ($((VNODES*5)) nodes) ==="
echo "    results : data/benchmarks/results/containerized/disaster_results_vnode_async_*.json"
