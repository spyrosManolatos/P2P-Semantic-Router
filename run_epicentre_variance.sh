#!/usr/bin/env bash
#
# run_epicentre_variance.sh
# Repeats the correlated-failure scenario at SEVERAL epicentres on one rung, so
# the resilience drop gets an error bar instead of being a single draw.
#
# WHY THIS EXISTS:
#   run_disaster_ladder.sh pins ONE epicentre per rung. Measured at N=50 that
#   gave a destroyed set of 38 clusters (semantic) vs 2 (clustered) -- expected
#   value K/N = 11 for BOTH. The blast radius is the target node's arc, and
#   Chord arc widths are ~exponentially distributed, so a single epicentre is a
#   very noisy draw. Comparing arms on one draw each measures arc-length luck as
#   much as architecture.
#
#   Two DIFFERENT effects are tangled in that difference, needing opposite
#   treatment:
#     * arc-width variance  -- noise. Same expectation in both arms. CONTROL it
#                              by averaging over epicentres. That is this script.
#     * density correlation -- real. Semantic's co-destroyed clusters are the
#                              anchor's SEMANTIC NEIGHBOURS, and topical
#                              popularity is smooth, so semantic's blast radius
#                              is systematically denser than random. REPORT it;
#                              do not average it away.
#
# EPICENTRE SELECTION (see scratchpad/select_epicentres.py):
#   Sampled with P(cluster) proportional to its document count. That is not
#   about the anchor's own size -- it induces node sampling proportional to each
#   node's TOTAL document load, since summing size(c)/total over the clusters a
#   node owns gives docs(node)/total. Uniform-over-IDs would instead mostly draw
#   near-empty clusters (median 11 docs, 15% under 5), where killing the owner
#   tests nothing. Applied identically to both arms, so it cannot favour either.
#
#   Anchors are kept >=20 cluster IDs apart: two anchors inside one node's arc
#   resolve to the SAME owner -- same arc, same blast radius -- a duplicate
#   sample, which defeats the averaging. Owners are verified distinct post hoc
#   from each result's target_node (the exact check; the ID gap is a heuristic).
#
#   Cluster 287 (the heaviest) is deliberately NOT in the sample -- including it
#   would bias the mean upward. It is the labelled worst case, measured by
#   run_disaster_ladder.sh, and is reported alongside this mean, not inside it.
#
# Usage:
#   ./run_epicentre_variance.sh                      # N=100, 5 epicentres, both arms
#   NUM_VNODES=10 ./run_epicentre_variance.sh        # N=50 instead
#   EPICENTRES="69,267" ./run_epicentre_variance.sh  # custom anchors
#
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CE="$ROOT/containerized_environment"
COMPOSE="$CE/docker-compose.async_vnodes.yml"
EVAL="src.benchmarks.containerized.evaluate"
RESULTS="$ROOT/data/benchmarks/results/containerized"

NUM_VNODES="${NUM_VNODES:-20}"        # 20/container x 5 = N=100 (the crossover rung)
N=$((NUM_VNODES * 5))
EPICENTRES="${EPICENTRES:-69,267,350,385,446}"
CORPUS_SIZE="${CORPUS_SIZE:-10000}"
K="${K:-550}"
NPROBES="${NPROBES:-1,5,20}"
ARCHES="${ARCHES:-async_semantic,async_clustered}"
CONTAINERS="av-bootstrap,av-node-1,av-node-2,av-node-3,av-node-4"
CONVERGE_TIMEOUT="${CONVERGE_TIMEOUT:-900}"
BOOT="${BOOT:-30}"
QFILE="${QFILE:-data/benchmarks/queries/queries_${CORPUS_SIZE}_uniform_500.json}"

cd "$CE"

if [[ "$K" == "5500" && "$CORPUS_SIZE" == "98104" ]]; then
  CONFIG_FILE="./config.vnodes.yaml"
else
  CONFIG_FILE="./config.vnodes.k${K}.n${CORPUS_SIZE}.yaml"
fi
export CONFIG_FILE
for f in "$CE/$CONFIG_FILE" "$ROOT/$QFILE"; do
  [[ -f "$f" ]] || { echo "!! missing $f -- run run_disaster_ladder.sh first to build artifacts." >&2; exit 1; }
done
echo ">>> N=$N | K=$K | corpus=$CORPUS_SIZE | epicentres: $EPICENTRES"

# Each epicentre is an INDEPENDENT trial: the kill is permanent by design (the
# replicas die with the primary), so the ring must be rebuilt and re-injected
# between trials rather than reused.
run_one () {  # arch epicentre
  local arch="$1" epi="$2"
  echo
  echo "=================================================================="
  echo ">>> [$arch] N=$N | epicentre cluster $epi"
  echo "=================================================================="
  ARCH="$arch" NUM_VNODES="$NUM_VNODES" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
  ARCH="$arch" NUM_VNODES="$NUM_VNODES" docker compose -f "$COMPOSE" up -d --build >/dev/null
  sleep "$BOOT"
  ARCH="$arch" NUM_VNODES="$NUM_VNODES" docker compose -f "$COMPOSE" run --rm runner \
    -m "$EVAL" --arch "$arch" --mode vnode_disaster \
    --containers "$CONTAINERS" --vnodes_per_container "$NUM_VNODES" \
    --dataset_size "$CORPUS_SIZE" --queries_file "$QFILE" \
    --nprobe_list "$NPROBES" --disaster_cluster "$epi" \
    --converge_timeout "$CONVERGE_TIMEOUT"
  ARCH="$arch" NUM_VNODES="$NUM_VNODES" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true

  local src="$RESULTS/disaster_results_vnode_${arch}.json"
  local tag="epivar_${arch}.K${K}.n${CORPUS_SIZE}.N${N}.epi${epi}.json"
  if [[ -f "$src" ]]; then
    mv "$src" "$RESULTS/$tag"; echo ">>> saved: $tag"
  else
    echo "!! no result for $arch at epicentre $epi" >&2
  fi
}

IFS=',' read -ra _archs <<< "$ARCHES"
IFS=',' read -ra _epis  <<< "$EPICENTRES"
for a in "${_archs[@]}"; do
  for e in "${_epis[@]}"; do run_one "$a" "$e"; done
done

# ------------------------------------------------------------------ summary --
echo
echo "=================================================================="
echo ">>> EPICENTRE VARIANCE -- drop with an error bar, not a single draw"
echo "=================================================================="
RESULTS="$RESULTS" K="$K" CORPUS_SIZE="$CORPUS_SIZE" NN="$N" python3 - <<'PY'
import json, os, glob, statistics as st

base, K, n, N = os.environ["RESULTS"], os.environ["K"], os.environ["CORPUS_SIZE"], os.environ["NN"]
for arch in ("async_semantic", "async_clustered"):
    rows = []
    for f in sorted(glob.glob(os.path.join(base, f"epivar_{arch}.K{K}.n{n}.N{N}.epi*.json"))):
        d = json.load(open(f))
        epi = f.split(".epi")[1].split(".json")[0]
        rows.append((epi, d.get("target_node"), len(d.get("killed_clusters") or []),
                     100 * d["concentration"]["mean_drop_all"],
                     100 * d["concentration"]["frac_affected"],
                     100 * d["concentration"]["mean_drop_affected"]))
    if not rows:
        print(f"\n  {arch}: no results"); continue
    print(f"\n  {arch}")
    print(f"  {'epi':>6} {'target node':>18} {'killed':>7} {'drop%':>7} {'affected%':>10} {'drop|affected%':>15}")
    for r in rows:
        print(f"  {r[0]:>6} {str(r[1]):>18} {r[2]:>7} {r[3]:>7.2f} {r[4]:>10.1f} {r[5]:>15.1f}")
    owners = [r[1] for r in rows]
    dup = len(owners) - len(set(owners))
    drops = [r[3] for r in rows]
    killed = [r[2] for r in rows]
    print(f"  -> drop   mean {st.mean(drops):.2f} pp"
          + (f" +/- {st.stdev(drops):.2f} sd" if len(drops) > 1 else ""))
    print(f"  -> killed mean {st.mean(killed):.1f} clusters"
          + (f" +/- {st.stdev(killed):.1f} sd" if len(killed) > 1 else "")
          + "   (this spread IS the arc-width noise being controlled)")
    if dup:
        print(f"  !! {dup} DUPLICATE target node(s) -- those trials are not independent; resample.")
    else:
        print("  -> all target nodes distinct: trials are independent.")
PY
echo
echo "=== EPICENTRE VARIANCE DONE (N=$N, epicentres: $EPICENTRES) ==="
