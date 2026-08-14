#!/usr/bin/env bash
#
# run_doomed_scenario.sh
# The Doomed Scenario: a deliberate, guaranteed-worst-case counterpart to
# run_vnode_disaster.sh. Instead of an arbitrary disaster epicenter and a
# random 50-query sample (most unaffected by luck), this targets the LARGEST
# K-Means cluster in the corpus and builds test queries FROM its own member
# courses, so their true top-1 match is guaranteed destroyed -- no luck
# needed for a signal. Sweeps nprobe much higher (up to 640) and reports
# hops alongside recall, since semantic's O(1) hop cost means high nprobe
# costs it almost nothing, unlike clustered whose hops grow with nprobe.
#
# Uses the ASYNC vnode stack (docker-compose.async_vnodes.yml / av-* containers),
# same as run_vnode_disaster.sh, for consistency with the async-only reporting
# scope.
#
# Usage:  ./run_doomed_scenario.sh [VNODES_PER_CONTAINER] [BOOT_SEC]
#   ./run_doomed_scenario.sh 20 30   -> 5x20 = 100-node ring (matches the headline hops run)
#   defaults: 20  30
#
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CE="$ROOT/containerized_environment"
COMPOSE="$CE/docker-compose.async_vnodes.yml"
EVAL="src.benchmarks.containerized.evaluate"

VNODES="${1:-20}"
BOOT="${2:-30}"
DS=98104
QFILE="data/benchmarks/queries/queries_98k_k4096.json"
# 3rd arg: comma-separated container subset (compose service names == container
# names), so the ring can be scaled up incrementally for diagnostics, e.g.
#   ./run_doomed_scenario.sh 2 20 av-bootstrap,av-node-1              -> 2x2 = 4 vnodes
#   ./run_doomed_scenario.sh 20 30                                    -> default 5x20 = 100
# 4th arg: comma-separated arch list (default: both).
CONTAINERS="${3:-av-bootstrap,av-node-1,av-node-2,av-node-3,av-node-4}"
ARCHES="${4:-async_semantic,async_clustered}"
SERVICES="${CONTAINERS//,/ }"
NCONT=$(printf '%s' "$CONTAINERS" | tr ',' '\n' | grep -c .)
# Trim the sweep / query count for fast diagnostic runs (env-overridable).
NPROBES="${NPROBES:-1,20,80,160,320,640}"
DOOMED_Q="${DOOMED_Q:-8}"
CONVERGE_TIMEOUT=900

cd "$CE"

run_arch () {  # arch
  local arch="$1"
  echo
  echo "=================================================================="
  echo ">>> [$arch] $((VNODES*NCONT))-node ring ($NCONT x $VNODES) | DOOMED | k=5500 | corpus $DS"
  echo ">>> nprobe sweep: $NPROBES | doomed queries: $DOOMED_Q"
  echo "=================================================================="
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" up -d --build $SERVICES
  echo ">>> booting ($BOOT s), then the benchmark's own convergence gates take over..."
  sleep "$BOOT"
  # --no-deps: the runner service depends_on ALL node services, so without this
  # `run` silently starts av-node-2..4 even when only a subset was launched --
  # the real ring then contains vnodes the benchmark was never told about, and
  # its successor walk steps outside the expected address set ("reaches 1/N").
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" run --rm --no-deps runner \
    -m "$EVAL" --arch "$arch" --mode doomed \
    --containers "$CONTAINERS" --vnodes_per_container "$VNODES" \
    --dataset_size "$DS" --queries_file "$QFILE" \
    --converge_timeout "$CONVERGE_TIMEOUT" \
    --nprobe_list "$NPROBES" --doomed_queries "$DOOMED_Q"
  ARCH="$arch" NUM_VNODES="$VNODES" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
}

IFS=',' read -ra _archs <<< "$ARCHES"
for _a in "${_archs[@]}"; do run_arch "$_a"; done

echo
echo "=== DOOMED SCENARIO RUN DONE ($((VNODES*NCONT)) nodes) ==="
echo "    results : data/benchmarks/results/containerized/doomed_scenario_results_async_*.json"
