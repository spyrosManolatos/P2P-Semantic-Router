#!/usr/bin/env bash
#
# run_all_benchmarks_fullcorpus.sh
# Full-98,104-course-corpus counterpart of run_all_benchmarks.sh: same 6
# architectures x 6 modes x fresh-ring-per-mode methodology, but injects the
# ENTIRE Kaggle corpus onto the 5-node ring instead of a 500-course subset,
# via the fast bulk_inject path (vectorized owner resolution + batched
# store_bulk RPCs) instead of sequential put_course calls.
#
# Reuses the existing full-corpus ground truth (queries/queries_98k_k4096.json,
# computed once via gen_queries_bigk.py) -- it's clustering-independent exact
# top-5 cosine similarity over the full corpus, so it's valid ground truth
# regardless of which architecture/k is doing the routing.
#
# Usage:   ./run_all_benchmarks_fullcorpus.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CE="$ROOT/containerized_environment"

NUM_NODES=5
DATASET_SIZE=98104
QFILE="data/benchmarks/queries/queries_98k_k4096.json"
STABILIZE_WAIT=20
EVAL="src.benchmarks.containerized.evaluate"

STACKS=(
  "async_semantic_router async_semantic async-semantic-runner"
  "async_clustered_dht   async_clustered async-clustered-runner"
)

echo "=================================================================="
echo ">>> Configuration"
echo "    corpus size   : $DATASET_SIZE (full Kaggle corpus)"
echo "    query set     : $QFILE"
echo "    ring nodes    : $NUM_NODES"
echo "    disaster_kills: 3 (default, RF+1 for RF=2)"
echo "=================================================================="

# load-balancing is deliberately excluded: the active-replica-delegation
# mechanism gets permanently stuck under any sustained query rate (see
# [[async-transport-migration]] memory), so it is not a viable result to
# report -- it is written up as future work instead of benchmarked.
MODES=(characterize scale fault join disaster)

run_stack () {
  local dir="$1" arch="$2" runner="$3"
  cd "$CE/$dir"
  # -f docker-compose.k5500.yml layers in CONFIG_PATH=config.vnodes.yaml (the
  # k=5500 centroid model already trained on this full corpus) on top of the
  # base compose file, instead of the default k=80 production centroids.
  local COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.k5500.yml)

  for mode in "${MODES[@]}"; do
    echo
    echo "=================================================================="
    echo ">>> [$arch / $mode] fresh ring in $dir (full corpus, k=5500)"
    echo "=================================================================="

    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
    "${COMPOSE[@]}" up -d

    echo ">>> [$arch / $mode] waiting ${STABILIZE_WAIT}s for the ring to stabilize..."
    sleep "$STABILIZE_WAIT"

    "${COMPOSE[@]}" run --rm --entrypoint python "$runner" \
      -m "$EVAL" --arch "$arch" --mode "$mode" \
      --num_nodes "$NUM_NODES" --dataset_size "$DATASET_SIZE" \
      --queries_file "$QFILE"
  done

  echo ">>> [$arch] tearing down"
  "${COMPOSE[@]}" down --remove-orphans
}

for entry in "${STACKS[@]}"; do
  # shellcheck disable=SC2086
  set -- $entry
  run_stack "$1" "$2" "$3"
done

echo
echo "=================================================================="
echo ">>> Regenerating comparison plots (matplotlib)"
echo "=================================================================="
cd "$CE/async_semantic_router"
docker compose -f docker-compose.yml -f docker-compose.k5500.yml run --rm --no-deps --entrypoint python async-semantic-runner \
  -m src.benchmarks.containerized.plot

echo
echo "=================================================================="
echo ">>> DONE."
echo "    results   : data/benchmarks/results/containerized/*.json"
echo "    plots     : data/benchmarks/plots/containerized/*.png"
echo "=================================================================="
