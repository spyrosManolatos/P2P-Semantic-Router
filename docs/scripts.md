# Benchmark Runner Scripts

Four shell scripts live at the project root. Each orchestrates one containerized
benchmark run end to end: it brings up a ring, injects the corpus, invokes
`src/benchmarks/containerized/evaluate.py` in a given `--mode`, tears the ring
down, and (for the 5-node suite) regenerates the comparison plots. This page is
the map: what each one runs, on what topology, and what it produces.

All four use the **async (FastAPI/httpx) stacks** — the `async_*` service
directories under `containerized_environment/`. The older synchronous
XML-RPC stacks and the `standard_dht` baseline have been retired: `standard_dht`
timed out at ring scale and is written up as a limitation rather than reported,
so the entire measured scope is **async semantic router vs. async clustered DHT**.

---

## End-to-end runbook (from nothing to a finished benchmark)

The scripts only orchestrate the *containers*. Three offline artifacts must
exist on the host first — the raw corpus, **the global centroid model every peer
holds**, and a query/ground-truth set. Steps 0–3 build them (host Python, run
once); step 4 runs the actual benchmark (Docker).

### 0. Prerequisites (once)

- **Docker + Docker Compose** — the benchmark rings run as containers.
- **A host Python env** for the offline generators:
  ```bash
  python -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  ```
- **The raw dataset.** These experiments use the *Udemy Courses Dataset* by
  Emre Bayir on Kaggle
  (`kaggle.com/datasets/emrebayirr/udemy-course-dataset-categories-ratings-and-trends`).
  Download it and place the CSV at **`data/raw/udemy_courses.csv`** (this is the
  only file you fetch by hand; everything under `data/` is gitignored).

### 1. Normalize the corpus

Parses the raw CSV into the unified `{course_id, course_title, description,
category}` schema every architecture ingests:

```bash
python src/scripts/ingest_kaggle_data.py
# data/raw/udemy_courses.csv  ->  data/raw/normalized_kaggle_courses.json  (~98,104 courses)
```

### 2. Train the global centroid model — *the artifact every peer holds*

This is the shared "topology" file. It contains the **TF-IDF vocabulary** plus
the **K-Means centroids**, agglomeratively **leaf-ordered** so that adjacent
cluster IDs are semantically adjacent (the property the semantic ring mapping
depends on — see [`pipeline.md`](pipeline.md), Phases 1–2). At boot **every peer
loads this exact same file into RAM**; it is what lets independent peers agree on
*where* a course belongs: each peer vectorizes text against the shared vocabulary
and places/routes it by nearest centroid. Without a single global model, peers
could not route consistently.

```bash
python src/ml/train_centroids_bigk.py --k 4096 \
    --data data/raw/normalized_kaggle_courses.json \
    --out  data/models/kaggle_centroids_k5500.json
```

*(The `_k5500` filename is a fixed historical label — the model trains `--k 4096`
centroids; every config and script references it by that name, so keep the path.)*

### 3. Generate the query + ground-truth sets

Exact top-5 cosine ground truth computed over the **full** corpus with
numpy/scipy (the pure-Python searcher would take hours at 98k docs). Two sets,
one per experiment family:

```bash
# hops / doomed / the 5-node suite:
python -m src.benchmarks.containerized.gen_queries_bigk \
    --centroids data/models/kaggle_centroids_k5500.json \
    --data      data/raw/normalized_kaggle_courses.json \
    --out        data/benchmarks/queries/queries_98k_k4096.json --queries 50

# uniform disaster (500 queries, each tagged with query_cluster + ground_truth_clusters):
python -m src.benchmarks.containerized.gen_queries_bigk \
    --centroids data/models/kaggle_centroids_k5500.json \
    --data      data/raw/normalized_kaggle_courses.json \
    --out        data/benchmarks/queries/queries_98k_uniform_500.json --queries 500
```

### 4. Run a benchmark

With the three artifacts in place, pick a script (details and the per-experiment
matrix below):

```bash
./run_all_benchmarks_fullcorpus.sh          # 5-node suite (+ regenerates plots)
./run_hops_scale_async.sh 20                # 100-node routing-hops headline
./run_vnode_disaster.sh 20 30               # 100-node uniform disaster
./run_doomed_scenario.sh 20 30              # 100-node worst case
```

Results land in `data/benchmarks/results/containerized/*.json`; the 5-node suite
also refreshes `data/benchmarks/plots/containerized/*.png`.

---

## Script reference

| Script | Experiment | Ring topology | Corpus / centroids | Produces |
|---|---|---|---|---|
| [`run_all_benchmarks_fullcorpus.sh`](../run_all_benchmarks_fullcorpus.sh) | The 5-node suite: `characterize`, `scale`, `fault`, `join`, `disaster` | **5-node** Compose, one fresh ring per mode | Full **98,104**-course corpus, `k=5500` centroids | `results_async_*`, `ring_characterization_async_*`, `fault_tolerance_results_async_*`, `node_join_results_async_*`, `disaster_results_async_*` + regenerated plots |
| [`run_hops_scale_async.sh`](../run_hops_scale_async.sh) | **The headline result**: routing hops vs. `nprobe` at scale | **50 / 100-node** virtual-node ring (5 containers × 10/20 vnodes) | Full corpus, `k=4096` GT | `hops_sweep_results_async_{semantic,clustered}.json` |
| [`run_vnode_disaster.sh`](../run_vnode_disaster.sh) | **Uniform-disaster distribution**: kill a correlated region, measure the *shape* of the damage | 100-node virtual-node ring (RF=2 replication kept on) | Full corpus, **500 uniform** cluster-tagged queries | `disaster_results_vnode_async_{semantic,clustered}.json` (per-query, per-cluster, region & concentration breakdowns) |
| [`run_doomed_scenario.sh`](../run_doomed_scenario.sh) | **Worst case**: queries built from the largest cluster's own members, so their true top-1 is guaranteed destroyed | 100-node virtual-node ring | Full corpus, `k=5500`, high `nprobe` sweep (up to 640) | `doomed_scenario_results_async_{semantic,clustered}.json` |

## Per-experiment artifact matrix

Exactly which corpus, model, peer count, topology, and query set each run uses.
All routing uses the same fine-grained centroid model
(`data/models/kaggle_centroids_k5500.json`, 5,500 centroids trained on the full
corpus); the `k4096` in a query filename names its ground-truth artifact, not a
second model.

| Experiment (script) | Mode(s) | Peers | Topology | Corpus | Centroid model | Query / GT file |
|---|---|---|---|---|---|---|
| 5-node suite (`run_all_benchmarks_fullcorpus.sh`) | `characterize`, `scale`, `fault`, `join`, `disaster` | **5** | 5 containers, 1 Chord id each (Compose) | 98,104 (full) | `kaggle_centroids_k5500.json` | `queries_98k_k4096.json` |
| Routing hops (`run_hops_scale_async.sh`) | `hops` | **50 / 100** | 5 containers × 10/20 vnodes (async_app_vnodes) | 98,104 (full) | `kaggle_centroids_k5500.json` | `queries_98k_k4096.json` |
| Uniform disaster (`run_vnode_disaster.sh`) | `vnode_disaster` | **100** | 5 containers × 20 vnodes, RF=2 | 98,104 (full) | `kaggle_centroids_k5500.json` | `queries_98k_uniform_500.json` (500, cluster-tagged) |
| Doomed worst-case (`run_doomed_scenario.sh`) | `doomed` | **100** | 5 containers × 20 vnodes | 98,104 (full) | `kaggle_centroids_k5500.json` | `queries_98k_k4096.json` (queries rebuilt from the largest cluster's members) |

Both architectures (`async_semantic`, `async_clustered`) run every experiment
with identical inputs; only the placement/routing strategy differs.

## The two ring topologies

**5-node Compose** (`run_all_benchmarks_fullcorpus.sh`) uses the per-architecture
stacks under `containerized_environment/async_{semantic_router,clustered_dht}/`,
layering `docker-compose.k5500.yml` on top of the base compose file to point the
nodes at the `k=5500` centroid model (`data/models/kaggle_centroids_k5500.json`).
Every non-routing Chapter 6 result — ring characterization, scaling, fault
tolerance, node join, and the small-ring disaster contrast — comes from here.

**Virtual-node ring** (the other three scripts) uses
`containerized_environment/async_app_vnodes.py` +
`docker-compose.async_vnodes.yml` + `config.vnodes.yaml`. Each of 5 containers
(`av-bootstrap`, `av-node-1..4`) hosts several independent Chord ring identities
(virtual nodes), so a handful of containers form a ring of 50 or 100+ members on
ordinary hardware. This exists because the routing-hops, disaster-distribution,
and doomed results are all **scale-dependent** — they demonstrate $O(\log N)$ vs.
$O(\text{nprobe}\cdot\log N)$ behaviour and correlated-failure blast radius, which
a 5-node ring cannot show.

## Usage

```bash
# --- 5-node full-corpus suite (characterize, scale, fault, join, disaster) ---
./run_all_benchmarks_fullcorpus.sh

# --- Routing-hops headline: 50 or 100 nodes, full corpus ---
./run_hops_scale_async.sh 10      # 5 x 10 = 50-node ring (recommended first)
./run_hops_scale_async.sh 20      # 5 x 20 = 100-node ring (the reported run)

# --- Uniform disaster distribution: 100 nodes, 500 cluster-tagged queries ---
./run_vnode_disaster.sh 20 30     # 5 x 20 = 100-node ring, 30s boot
#   env overrides for the krylov large run (no code change):
#   QFILE=... NPROBES=1,5,20 DISASTER_CLUSTER=-1 DS=98104 ARCHES=async_semantic,async_clustered

# --- Doomed worst-case: 100 nodes, high nprobe sweep ---
./run_doomed_scenario.sh 20 30
#   diagnostic subset:  ./run_doomed_scenario.sh 2 20 av-bootstrap,av-node-1
#   env overrides:      NPROBES=1,20,80,160,320,640 DOOMED_Q=8
```

## Convergence

The virtual-node scripts do not rely on a fixed sleep. After a short boot delay
they hand off to the benchmark harness's own convergence gates — first a single
valid successor cycle (correctness), then per-node finger-table quiescence — via
`--converge_timeout` (900s). Large rings take several minutes to settle and the
harness will not measure until they have; at 100 vnodes it may report e.g.
"97/100 nodes quiescent" and proceed on a small residual, which does not affect
recall. The 5-node suite uses a fixed `STABILIZE_WAIT` (20s), adequate at that
scale.

## Inputs each run depends on

- **Centroids**: `data/models/kaggle_centroids_k5500.json` (trained via
  `src/ml/train_centroids_bigk.py`). The 5-node suite and doomed run use it as
  the routing model; the hops run measures against `k=4096` ground truth.
- **Query / ground-truth files** (in `data/benchmarks/queries/`, all with exact
  full-corpus top-5 cosine GT from `src/benchmarks/containerized/gen_queries_bigk.py`):
  - `queries_98k_k4096.json` — hops, doomed, and the 5-node suite.
  - `queries_98k_uniform_500.json` — the uniform disaster run; 500 queries each
    tagged with `query_cluster` + `ground_truth_clusters` so damage can be
    grouped by topic community.
- **Outputs**: `data/benchmarks/results/containerized/*.json` and, for the 5-node
  suite, `data/benchmarks/plots/containerized/*.png`.

## Note: `load` mode and `standard` are excluded on purpose

The `load` (load-balancing) mode is omitted because the active-replica-delegation
mechanism stalls under sustained query rate, so it is discussed as future work
rather than benchmarked, and the `standard_dht` architecture is retired as above.
Both are already dropped from the runner scripts and from the matplotlib plotter
(`src/benchmarks/containerized/plot.py`), which reads only the `*_async_*` result
files and emits five figures (scaling, fault, node-join, disaster contrast, and
the hops-vs-nprobe headline) — no `load_balancing` or `standard` series.
