#!/usr/bin/env bash
#
# run_disaster_ladder.sh
# Granularity ladder for correlated failure: does the Semantic Router's
# resilience penalty shrink as the ring densifies (K/N -> 1)?
#
# The two existing disaster runs (5-node sparse, 100-vnode dense) differ in
# THREE variables at once -- node count, fraction of the ring destroyed, and
# query set -- so their 20,1pp -> 4,7pp narrowing cannot be attributed to
# granularity alone. This script varies N and nothing else.
#
# HELD FIXED across every rung (but configurable ACROSS ladder runs, see
# CORPUS_SIZE / K below):
#   * query set          -- the cluster-tagged uniform set, same file every N
#   * artifact + RF      -- picked by K, RF=2
#   * nprobe list        -- same fanout points
#   * semantic epicentre -- the owner of the SINGLE HEAVIEST CLUSTER, pinned by
#                           cluster id (--disaster_cluster), so every rung wipes
#                           out the same topic region. Using the default -1
#                           ("heaviest node") would instead pick whichever arc
#                           happens to be fattest at that N -- a different topic
#                           region per rung, hence not comparable.
#   * kill count         -- RF+1 = 3 ring-adjacent nodes
#
# WHY A FIXED KILL COUNT AND NOT A FIXED KILL FRACTION:
#   Killing a contiguous f% of the ring destroys f% of cluster space at EVERY N,
#   which holds the destroyed arc constant -- and the destroyed arc IS the
#   Semantic Router's blast radius, so fraction-matching would erase the very
#   effect under test. A fixed machine count is also the operationally honest
#   model: a rack holds the same number of machines however the ring was sized.
#   Volume stays controlled WITHIN each rung, because both architectures lose
#   the same three nodes' worth of data -- so the semantic-minus-clustered GAP
#   is a pure shape-of-loss measurement, independent of how much was destroyed.
#   That gap, not the absolute drop, is the quantity to read off the ladder.
#
# Usage:
#   ./run_disaster_ladder.sh                  # default ladder 10 20 40 (N=50,100,200)
#   LADDER="10 20" ./run_disaster_ladder.sh   # custom rungs (vnodes per container)
#   CLUSTER_ID=1234 ./run_disaster_ladder.sh  # skip the preflight, pin this cluster
#   CORPUS_SIZE=10000 K=550 ./run_disaster_ladder.sh   # smaller pilot artifact
#
# CORPUS_SIZE and K: neither has to pre-exist. Both the centroid artifact and
# the query set are generated on the fly (host venv) the first time a given
# (K, CORPUS_SIZE) pair is requested, then reused on every later run:
#   * K            -> if data/models/kaggle_centroids_k${K}[_n${CORPUS_SIZE}].json
#                     is missing, it is TRAINED (src/ml/train_centroids_bigk.py)
#                     against the first CORPUS_SIZE courses -- i.e. exactly the
#                     subset this rung actually injects, so cluster granularity
#                     matches the scenario instead of being diluted against the
#                     full 98k.
#   * CORPUS_SIZE  -> bulk_inject only ever loads the first CORPUS_SIZE courses
#                     (MonolithicSearcher's `courses[:limit]` order), so the
#                     ground-truth query set must be built against that SAME
#                     subset -- a query file generated for a different corpus
#                     size could reference courses outside it. Default 98104
#                     reuses the existing queries_98k_uniform_500.json; any
#                     other size is generated fresh with gen_queries_bigk.py
#                     against the same sliced corpus and centroids as the K
#                     artifact above (QUERIES, default 500, controls count).
#
# Each rung tags its output as
#   disaster_results_vnode_async_{semantic,clustered}.K<K>.n<CORPUS_SIZE>.N<nodes>.json
# because evaluate.py writes a fixed filename that later rungs (or a
# differently-configured ladder run) would otherwise overwrite.
#
set -uo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CE="$ROOT/containerized_environment"
COMPOSE="$CE/docker-compose.async_vnodes.yml"
EVAL="src.benchmarks.containerized.evaluate"
RESULTS="$ROOT/data/benchmarks/results/containerized"

LADDER="${LADDER:-10 20 40}"          # vnodes per container; N = 5 x this
BOOT="${BOOT:-30}"
CORPUS_SIZE="${CORPUS_SIZE:-98104}"   # courses injected into every rung (n)
K="${K:-5500}"                        # centroid count -- selects artifact + config
NPROBES="${NPROBES:-1,5,20}"
ARCHES="${ARCHES:-async_semantic,async_clustered}"
CONTAINERS="av-bootstrap,av-node-1,av-node-2,av-node-3,av-node-4"
CONVERGE_TIMEOUT="${CONVERGE_TIMEOUT:-900}"

cd "$CE"

# ------------------------------------------------------------- K artifact --
# Artifact identity is (K, CORPUS_SIZE): training on the full 98k corpus at a
# small K gives production docs/cluster, not the granularity a smaller-corpus
# scenario is meant to probe. So a non-default CORPUS_SIZE gets its own
# artifact, trained on exactly the first CORPUS_SIZE courses -- the same
# subset bulk_inject loads for this scenario.
PYBIN="$ROOT/.venv/bin/python3"
[[ -x "$PYBIN" ]] || PYBIN="python3"

if [[ "$CORPUS_SIZE" == "98104" ]]; then
  CENTROIDS_REL="data/models/kaggle_centroids_k${K}.json"
  TRAIN_DATA_REL="data/raw/normalized_kaggle_courses.json"
else
  CENTROIDS_REL="data/models/kaggle_centroids_k${K}_n${CORPUS_SIZE}.json"
  TRAIN_DATA_REL="data/raw/normalized_kaggle_courses.n${CORPUS_SIZE}.json"
  if [[ ! -f "$ROOT/$TRAIN_DATA_REL" ]]; then
    echo ">>> slicing first $CORPUS_SIZE courses -> $TRAIN_DATA_REL"
    "$PYBIN" -c "
import json
courses = json.load(open('$ROOT/data/raw/normalized_kaggle_courses.json'))
if len(courses) < $CORPUS_SIZE:
    raise SystemExit(f'corpus only has {len(courses)} courses, cannot slice $CORPUS_SIZE')
json.dump(courses[:$CORPUS_SIZE], open('$ROOT/$TRAIN_DATA_REL', 'w'))
"
  fi
fi

if [[ ! -f "$ROOT/$CENTROIDS_REL" ]]; then
  echo ">>> no centroid artifact for K=$K, CORPUS_SIZE=$CORPUS_SIZE -- training it now"
  echo "    ($TRAIN_DATA_REL -> $CENTROIDS_REL, host venv, this is the slow step)"
  ( cd "$ROOT" && "$PYBIN" src/ml/train_centroids_bigk.py \
      --k "$K" --data "$TRAIN_DATA_REL" --out "$CENTROIDS_REL" )
  if [[ ! -f "$ROOT/$CENTROIDS_REL" ]]; then
    echo "!! training did not produce $CENTROIDS_REL, aborting." >&2
    exit 1
  fi
fi

# NOTE the leading "./": compose's short volume syntax treats a bare filename as
# a NAMED VOLUME, not a bind mount ("refers to undefined volume ..."), so the
# path must be explicitly relative for the override to mount the real file.
if [[ "$K" == "5500" && "$CORPUS_SIZE" == "98104" ]]; then
  CONFIG_FILE="./config.vnodes.yaml"   # the existing, checked-in default config
else
  CONFIG_FILE="./config.vnodes.k${K}.n${CORPUS_SIZE}.yaml"
  if [[ ! -f "$CE/$CONFIG_FILE" ]]; then
    echo ">>> generating $CONFIG_FILE (centroids -> $CENTROIDS_REL)"
    sed "s#kaggle_dataset_path: \".*\"#kaggle_dataset_path: \"$CENTROIDS_REL\"#" \
      "$CE/config.vnodes.yaml" > "$CE/$CONFIG_FILE"
  fi
fi
export CONFIG_FILE
echo ">>> K=$K, CORPUS_SIZE=$CORPUS_SIZE -> $CENTROIDS_REL via $CONFIG_FILE"

# ------------------------------------------------------------- N query set --
QUERIES="${QUERIES:-500}"
if [[ "$CORPUS_SIZE" == "98104" ]]; then
  QFILE="${QFILE:-data/benchmarks/queries/queries_98k_uniform_500.json}"
else
  QFILE="${QFILE:-data/benchmarks/queries/queries_${CORPUS_SIZE}_uniform_${QUERIES}.json}"
fi
if [[ ! -f "$ROOT/$QFILE" ]]; then
  echo ">>> no query set for CORPUS_SIZE=$CORPUS_SIZE -- generating it now"
  echo "    ($TRAIN_DATA_REL + $CENTROIDS_REL -> $QFILE, $QUERIES queries, host venv)"
  ( cd "$ROOT" && "$PYBIN" -m src.benchmarks.containerized.gen_queries_bigk \
      --centroids "$CENTROIDS_REL" --data "$TRAIN_DATA_REL" \
      --out "$QFILE" --queries "$QUERIES" )
  if [[ ! -f "$ROOT/$QFILE" ]]; then
    echo "!! generation did not produce $QFILE, aborting." >&2
    exit 1
  fi
fi
echo ">>> CORPUS_SIZE=$CORPUS_SIZE -> $QFILE"

# ---------------------------------------------------------------- preflight --
# Resolve the heaviest cluster ONCE, on the host corpus, and reuse that id for
# every rung. Runs inside the runner image so it uses the same artifact and the
# same assignment arithmetic as bulk_load_direct (chunked numpy argmin over the
# centroid matrix), rather than a second implementation that could drift.
if [[ -z "${CLUSTER_ID:-}" ]]; then
  echo ">>> preflight: locating the heaviest cluster (once, reused by every rung)"
  PREFLIGHT_ERR="$(mktemp)"
  # CORPUS_SIZE must be injected with `-e`: `docker compose run` does NOT forward
  # the host environment into the container, so a bare VAR=... prefix reaches the
  # compose CLI (where CONFIG_FILE is interpolated) but never the python below.
  CLUSTER_ID=$(ARCH=async_semantic NUM_VNODES=1 CONFIG_FILE="$CONFIG_FILE" \
    docker compose -f "$COMPOSE" \
    run --rm --no-deps -e CORPUS_SIZE="$CORPUS_SIZE" runner -c '
import json, os, sys, numpy as np
sys.path.append("/app/src")
os.environ.setdefault("CONFIG_PATH", "/app/containerized_environment/config.vnodes.yaml")
from core.config_loader import load_config
cfg = load_config()
art = json.load(open(cfg["storage"]["centroids"]["kaggle_dataset_path"]))
courses = json.load(open(cfg["storage"]["data"]["kaggle"]["normalized_path"]))
courses = courses[:int(os.environ["CORPUS_SIZE"])]  # mirror MonolithicSearcher(limit=...)
vocab, nfeat = art["vocabulary"], art["n_features"]
C = np.asarray(art["centroids"], dtype=np.float32)
cn2 = (C * C).sum(axis=1)
counts = np.zeros(len(C), dtype=np.int64)
CH = 2000
for s in range(0, len(courses), CH):
    chunk = courses[s:s + CH]
    X = np.zeros((len(chunk), nfeat), dtype=np.float32)
    for r, c in enumerate(chunk):
        # Built by concatenation, not an f-string: this block is embedded in a
        # single-quoted shell string, so escaping the inner quotes would put
        # backslashes inside an f-string expression (a SyntaxError).
        text = c["course_title"] + " " + c["category"] + " " + c["description"]
        for tok in text.lower().split():
            j = vocab.get(tok)
            if j is not None:
                X[r, j] += 1.0
    n = np.sqrt((X * X).sum(axis=1)); n[n == 0] = 1.0; X /= n[:, None]
    ids = (cn2 - 2.0 * (X @ C.T)).argmin(axis=1)
    np.add.at(counts, ids, 1)
top = int(counts.argmax())
print(f"HEAVIEST {top} {int(counts[top])}", file=sys.stderr)
print(top)
' 2>"$PREFLIGHT_ERR" | tr -d "[:space:]")
  # The python reports "HEAVIEST <id> <count>" on stderr -- surface it, since the
  # cluster's actual weight is worth seeing before committing to a whole ladder.
  grep -E "^HEAVIEST" "$PREFLIGHT_ERR" || true
fi

if ! [[ "$CLUSTER_ID" =~ ^[0-9]+$ ]]; then
  echo "!! preflight failed to resolve a cluster id (got: '${CLUSTER_ID}')." >&2
  if [[ -n "${PREFLIGHT_ERR:-}" && -f "$PREFLIGHT_ERR" ]]; then
    echo "   --- preflight stderr (tail) ---" >&2
    tail -15 "$PREFLIGHT_ERR" >&2
  fi
  echo "   Re-run with CLUSTER_ID=<id> to pin it manually." >&2
  exit 1
fi
echo ">>> epicentre pinned: cluster $CLUSTER_ID (same topic region at every rung)"

# -------------------------------------------------------------------- rungs --
run_rung () {  # vnodes_per_container arch
  local vn="$1" arch="$2" n=$((vn * 5))
  echo
  echo "=================================================================="
  echo ">>> [$arch] N=$n ($vn/container) | epicentre cluster $CLUSTER_ID | K=$K | RF=2"
  echo ">>> K/N = $((K / n)) clusters per node"
  echo "=================================================================="
  ARCH="$arch" NUM_VNODES="$vn" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true
  ARCH="$arch" NUM_VNODES="$vn" docker compose -f "$COMPOSE" up -d --build
  echo ">>> booting (${BOOT}s), then the benchmark's own convergence gates take over..."
  sleep "$BOOT"
  ARCH="$arch" NUM_VNODES="$vn" docker compose -f "$COMPOSE" run --rm runner \
    -m "$EVAL" --arch "$arch" --mode vnode_disaster \
    --containers "$CONTAINERS" --vnodes_per_container "$vn" \
    --dataset_size "$CORPUS_SIZE" --queries_file "$QFILE" \
    --nprobe_list "$NPROBES" --disaster_cluster "$CLUSTER_ID" \
    --converge_timeout "$CONVERGE_TIMEOUT"
  ARCH="$arch" NUM_VNODES="$vn" docker compose -f "$COMPOSE" down --remove-orphans >/dev/null 2>&1 || true

  # evaluate.py writes a fixed filename per arch; tag it with K/CORPUS_SIZE/N so
  # neither the next rung nor a differently-configured ladder run overwrites it.
  local src="$RESULTS/disaster_results_vnode_${arch}.json"
  local tag="disaster_results_vnode_${arch}.K${K}.n${CORPUS_SIZE}.N${n}.json"
  if [[ -f "$src" ]]; then
    mv "$src" "$RESULTS/$tag"
    echo ">>> saved: $tag"
  else
    echo "!! no result file produced for $arch at N=$n" >&2
  fi
}

IFS=',' read -ra _archs <<< "$ARCHES"
for vn in $LADDER; do
  for a in "${_archs[@]}"; do run_rung "$vn" "$a"; done
done

# ------------------------------------------------------------------ summary --
echo
echo "=================================================================="
echo ">>> LADDER SUMMARY -- the gap is the measurement, not the drop"
echo "=================================================================="
RESULTS="$RESULTS" K="$K" CORPUS_SIZE="$CORPUS_SIZE" python3 - <<'PY'
import json, os, glob
# Absolute: this heredoc runs with cwd at containerized_environment/, not $ROOT.
base = os.environ["RESULTS"]
K = int(os.environ["K"])
pattern = f"disaster_results_vnode_async_semantic.K{K}.n{os.environ['CORPUS_SIZE']}.N*.json"
rows = []
for f in sorted(glob.glob(os.path.join(base, pattern))):
    n = int(f.split(".N")[1].split(".json")[0])
    clus = f.replace("async_semantic", "async_clustered")
    if not os.path.exists(clus):
        continue
    s, c = json.load(open(f)), json.load(open(clus))
    sd = s["concentration"]["mean_drop_all"] * 100
    cd = c["concentration"]["mean_drop_all"] * 100
    # Routing cost at the widest fanout in the sweep, healthy ring.
    sh = s.get("baseline_hops_by_nprobe") or [0]
    ch = c.get("baseline_hops_by_nprobe") or [0]
    rows.append((n, K / n, sd, cd, sd - cd, sh[-1], ch[-1],
                 (ch[-1] / sh[-1]) if sh[-1] else 0.0))
if not rows:
    print("  (no paired results found -- did both arches run?)")
else:
    print("  RESILIENCE -- gap between the two placements at each rung")
    print(f"  {'N':>6} {'K/N':>7} {'sem drop':>9} {'clus drop':>10} {'GAP(pp)':>9}")
    for n, kn, sd, cd, gap, _, _, _ in rows:
        print(f"  {n:>6} {kn:>7.1f} {sd:>8.2f}p {cd:>9.2f}p {gap:>9.2f}")
    print()
    print("  GAP should decay toward 0 as K/N -> 1 if the penalty is an artifact")
    print("  of coarse arcs. Both arches lose the same 3 nodes at each rung, so")
    print("  the gap is volume-controlled and measures shape of loss only.")
    print()
    print("  ROUTING -- healthy-ring hops at the widest nprobe in the sweep")
    print(f"  {'N':>6} {'K/N':>7} {'sem hops':>9} {'clus hops':>10} {'ratio':>8}")
    for n, kn, _, _, _, sh, ch, ratio in rows:
        print(f"  {n:>6} {kn:>7.1f} {sh:>9.2f} {ch:>10.2f} {ratio:>7.1f}x")
    print()
    print("  Semantic should track O(log N + nprobe) and clustered")
    print("  O(nprobe * log N). NOTE: semantic hops are expected to GROW with N --")
    print("  the flat curve at N=100 is the sparse degenerate case, where a whole")
    print("  nprobe fanout fits inside one node's arc. What must hold is the")
    print("  SEPARATION, not flatness.")
PY
echo
echo "=== DISASTER LADDER DONE (rungs: $LADDER vnodes/container) ==="
