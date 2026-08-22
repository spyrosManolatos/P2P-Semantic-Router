#!/usr/bin/env bash
#
# run_nk_sweep.sh
# Real-ring validation of the N/K ratio (clusters-per-node) ladder.
#
# The offline model predicts that the Semantic Router's correlated-failure
# penalty collapses as c = K/N falls from ~55 to ~1. This sweep measures it on
# a live ring, one point per predicted point, so every run either confirms or
# refutes a specific number rather than exploring blind.
#
# Ring size is CONTAINERS x VNODES (see gen_compose_vnodes.py for why both
# matter). The default ladder holds K = 5500 fixed -- the thesis artifact --
# and varies N, so c is the only moving variable.
#
#   ./run_nk_sweep.sh                 # full ladder
#   PAIRS="5:20 22:25" ./run_nk_sweep.sh   # just N=100 and N=550
#   DRY_RUN=1 ./run_nk_sweep.sh       # print the plan, touch nothing
#
# On a 48-core host keep CONTAINERS <= 48; beyond that the processes contend
# and only convergence time suffers (hop counts are messages, not timings).
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CE="$ROOT/containerized_environment"
RESULTS="$ROOT/data/benchmarks/results/containerized"
GEN="$CE/gen_compose_vnodes.py"
EVAL="src.benchmarks.containerized.evaluate"

# containers:vnodes -> N. Mirrors the offline ladder: 100, 275, 550, 1375, 2750, 5500.
PAIRS="${PAIRS:-5:20 11:25 22:25 25:55 50:55 50:110}"
ARCHES="${ARCHES:-async_semantic async_clustered}"
DS="${DS:-98104}"
DISASTER_CLUSTER="${DISASTER_CLUSTER:--1}"  # -1 = kill the HEAVIEST node (size-driven, so the
                                 # victim differs between arms and damage is not controlled).
                                 # >=0 kills the owner of THAT cluster in both arms, so the
                                 # targeted CONTENT is identical and the difference in outcome
                                 # is attributable to placement. 5495 = biggest AND hottest
                                 # cluster (1619 docs, 52/500 production queries).
QFILE="${QFILE:-data/benchmarks/queries/queries_98k_uniform_500.json}"
KCLUSTERS="${KCLUSTERS:-5500}" # cluster count in the mounted artifact; only used to
                                 # spread the healthcheck probe ids across the id space
CPU_BUDGET="${CPU_BUDGET:-0.6}"   # fraction of host cores to aim for IN TOTAL. This is
                                 # courtesy on a shared, scheduler-less box: `nice` makes
                                 # us yield under contention, but a visible load of ~ncores
                                 # still looks like we own the machine. Raise to 1.0 when
                                 # the box is genuinely ours.
SUBNET="${SUBNET:-172.28.0.0/16}"  # pins ring addresses. Node id = hash(IP), so a
                                 # DIFFERENT subnet = a different ring draw. Keep it
                                 # fixed across rungs or c and topology confound.
CONCURRENCY="${CONCURRENCY:-8}"  # queries in flight; 1 = the old sequential loop
QTIMEOUT="${QTIMEOUT:-120}"      # per-RPC timeout (s); must exceed the slowest query
                                 # under load, or timeouts masquerade as zero recall
NPROBE="${NPROBE:-5}"
NPROBE_LIST="${NPROBE_LIST:-1,5,20}"
KILLS="${KILLS:-3}"              # target + RF successors: RF=2 => 3
STAGGER="${STAGGER:-2.0}"        # per-vnode join delay -- MUST match async_app_vnodes.py default.
                                 # 0.5 joins faster than stabilize_interval_sec=1.0 can repair,
                                 # leaving finger tables malformed. Semantic tolerates that (walks
                                 # successors); clustered collapses (needs long O(log N) jumps).
DRY_RUN="${DRY_RUN:-0}"

# Predicted post-disaster recall from the offline model, for live comparison.
declare -A PRED_SEM=( [100]=0.6613 [275]=0.6894 [550]=0.6995 [1375]=0.7076 [2750]=0.7095 [5500]=0.7117 )
declare -A PRED_CLU=( [100]=0.6811 [275]=0.7040 [550]=0.7104 [1375]=0.7125 [2750]=0.7130 [5500]=0.7132 )

banner () { printf '\n=========================================================\n%s\n=========================================================\n' "$1"; }

echo "N/K sweep plan   (K = 5500, held fixed; baseline recall 0.7208)"
printf '%8s %8s %6s %12s %12s\n' N containers vnodes pred_sem pred_clu
for pair in $PAIRS; do
  C="${pair%%:*}"; V="${pair##*:}"; N=$((C * V))
  printf '%8s %8s %6s %12s %12s\n' "$N" "$C" "$V" "${PRED_SEM[$N]:-?}" "${PRED_CLU[$N]:-?}"
done
echo
[ "$DRY_RUN" = "1" ] && { echo "DRY_RUN=1 -- stopping before any container is started."; exit 0; }

command -v docker >/dev/null || { echo "docker not found on PATH"; exit 1; }
[ -f "$ROOT/$QFILE" ] || { echo "missing query file: $QFILE"; exit 1; }
mkdir -p "$RESULTS"

cd "$CE"
for pair in $PAIRS; do
  C="${pair%%:*}"; V="${pair##*:}"; N=$((C * V))
  COMPOSE="$CE/docker-compose.generated.N${N}.yml"

  # Container list must match what evaluate.py enumerates.
  # BLAS inside each container otherwise spawns one thread PER HOST CORE, so a
  # 50-container ring on a 48-core box would ask for ~2400 threads. Give each
  # container an equal slice instead; the parallelism that matters at large N
  # comes from having many containers, not from threads inside each one.
  BLAS=$(python3 -c "import os;print(max(1, int((os.cpu_count() or 8) * $CPU_BUDGET) // $C))")
  CLIST="$(python3 "$GEN" --containers "$C" --vnodes "$V" --out "$COMPOSE" \
             --stagger "$STAGGER" --subnet "$SUBNET" --blas-threads "$BLAS" \
             | sed -n 's/^CONTAINERS=//p')"
  [ -n "$CLIST" ] || { echo "generator produced no container list for N=$N"; continue; }

  # Boot: every container staggers its own vnodes, so they overlap; the
  # benchmark's convergence gates are what actually decide readiness.
  BOOT=$(python3 -c "print(int(30 + $STAGGER * $V * 1.5))")
  CONVERGE=$(python3 -c "print(min(5400, int(600 + 1.2 * $N)))")

  for arch in $ARCHES; do
    banner ">>> N=$N (${C}c x ${V}v, c=K/N=$(python3 -c "print(round(5500/$N,1))")) | $arch"
    ARCH="$arch" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1
    ARCH="$arch" docker compose -f "$COMPOSE" up -d --build || { echo "up failed, skipping"; continue; }
    echo ">>> booting ${BOOT}s, then convergence gates (timeout ${CONVERGE}s) take over..."
    sleep "$BOOT"

    # Functional pre-flight: the convergence gates test whether fingers stopped
    # CHANGING; this tests whether they are CORRECT. At N>=275 the former is never
    # observed (fix_fingers runs continuously) so the gate times out and proceeds --
    # and a malformed-finger ring silently returns nothing for CLUSTERED while
    # looking fine for SEMANTIC. Refuse the rung instead of publishing that.
    FIRST_C="${CLIST%%,*}"
    if ! docker exec "$FIRST_C" python /app/src/benchmarks/containerized/ring_healthcheck.py \
           --arch "$arch" --containers "$CLIST" \
           --vnodes_per_container "$V" --k "$KCLUSTERS"; then
      echo "  !! SKIPPING N=$N [$arch] -- ring failed the functional healthcheck."
      ARCH="$arch" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
      continue
    fi

    ARCH="$arch" docker compose -f "$COMPOSE" run --rm --no-deps runner -m "$EVAL" \
      --arch "$arch" --mode vnode_disaster \
      --containers "$CLIST" --vnodes_per_container "$V" \
      --dataset_size "$DS" --queries_file "$QFILE" \
      --nprobe "$NPROBE" --nprobe_list "$NPROBE_LIST" \
      --disaster_kills "$KILLS" --disaster_cluster "$DISASTER_CLUSTER" \
      --query_concurrency "$CONCURRENCY" --query_timeout "$QTIMEOUT" \
      --converge_timeout "$CONVERGE"
    rc=$?

    SRC="$RESULTS/disaster_results_vnode_${arch}.json"
    DST="$RESULTS/disaster_results_vnode_${arch}.N${N}.json"
    if [ $rc -eq 0 ] && [ -f "$SRC" ]; then
      mv "$SRC" "$DST"
      python3 - "$DST" "$N" "${PRED_SEM[$N]:-}" "${PRED_CLU[$N]:-}" "$arch" <<'PY'
import json, sys
path, n, ps, pc, arch = sys.argv[1:6]
d = json.load(open(path))
pred = ps if arch.endswith("semantic") else pc
b, a = d.get("baseline_recall"), d.get("disaster_recall")
line = f"  N={n} {arch}: baseline {b} -> disaster {a}"
if pred:
    line += f"   (predicted {pred}, delta {float(a) - float(pred):+.4f})"
print(line)
print(f"  affected: {d.get('concentration', {}).get('n_affected')}"
      f"/{d.get('concentration', {}).get('n_queries')}"
      f"   killed clusters: {d.get('region_summary', {}).get('killed_cluster_count')}")
PY
    else
      echo "  !! run failed (rc=$rc) or no result file; leaving ring notes above"
    fi

    ARCH="$arch" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1
  done
done

banner "N/K SWEEP DONE"
echo "results: $RESULTS/disaster_results_vnode_async_*.N*.json"
