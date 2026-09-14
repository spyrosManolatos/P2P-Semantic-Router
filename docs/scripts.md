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

## Script Reference

The repository provides automated shell scripts at the project root to reproduce every experiment in **Chapter 6 of the thesis**:

| Script | Thesis Section | Experiment & Ring Topology | Corpus & Centroids | Output Artifacts |
|---|---|---|---|---|
| [`run_all_benchmarks_fullcorpus.sh`](../run_all_benchmarks_fullcorpus.sh) | **6.2, 6.3, 6.4, 6.5, 6.7, 6.8.1** | 5-node suite (`characterize`, `scale`, `fault`, `join`, `disaster`) on 5 Docker containers | Full **98,104** courses, $K=5,500$ | `results_async_*`, `ring_characterization_async_*`, `fault_tolerance_results_async_*`, `node_join_results_async_*`, `disaster_results_async_*` + Figs 6.1, 6.3, 6.4, 6.5 |
| [`run_hops_scale_async.sh`](../run_hops_scale_async.sh) | **6.3.1** | **Routing hops at scale**: 100-node virtual ring (5 containers × 20 vnodes) | Full corpus, $K=5,500$ | `hops_sweep_results_async_{semantic,clustered}.json` + Fig 6.2 |
| [`run_vnode_disaster.sh`](../run_vnode_disaster.sh) | **6.8.2** | **Dense-ring hot-node disaster distribution**: 100 vnodes, 500 uniform queries | Full corpus, $K=5,500$ | `disaster_results_vnode_async_{semantic,clustered}.100nodes_uniform500.json` + Fig 6.6 |
| [`run_doomed_scenario.sh`](../run_doomed_scenario.sh) | **6.8.3** | **Worst-case doomed queries**: 100 vnodes, sweep $nprobe \in [1, 640]$ | Full corpus, $K=5,500$ | `doomed_scenario_results_async_{semantic,clustered}.json` + Fig 6.7 |
| [`run_disaster_ladder.sh`](../run_disaster_ladder.sh) | **6.8.4** | **The scale of $c = K/N$**: 5, 25, 100, 275 vnodes, 500 Zipf queries | Full corpus, $K=5,500$ | `ladder/prod.{semantic,clustered}.N*.json` + Fig 6.8 |
| [`run_epicentre_variance.sh`](../run_epicentre_variance.sh) | **6.8.4** | **Ring draw geometry variance**: 2nd independent draw for $N=25, 100$ | Full corpus, $K=5,500$ | `ladder/prod.{semantic,clustered}.N*.draw2.json` |
| [`run_nk_sweep.sh`](../run_nk_sweep.sh) | **6.8.4 / 7.3** | Density & parameter sweep across ring sizes and centroid ratios | Variable subsets / $K$ | Parameter exploration JSON dumps |
| [`run_nk_sweep_ip.sh`](../run_nk_sweep_ip.sh) | **6.8.4 / 7.3** | Fixed IP-deterministic variation of the $N/K$ density sweep | Variable topologies | Multi-rung density logs |

## Per-Experiment Artifact Matrix (Thesis Table 6.1)

Direct mapping to **Table 6.1 ("Δεδομένα, περιβάλλον, αρχιτεκτονικές, παράμετροι και αρχείο εξόδου ανά πείραμα")**:

| Experiment (Section) | Environment | Architectures | Key Parameters | Output Artifact (in `data/benchmarks/results/containerized/`) |
|---|---|---|---|---|
| Ring characterization (6.2) | 5 nodes, Compose | Semantic, Clustered | 50 queries; load / adjacency analysis | `ring_characterization_async_*` |
| Routing efficiency (6.3) | 5 nodes, Compose | Semantic, Clustered, (Mono) | $nprobe \in [1, 80]$; 50 queries | `results_async_*` |
| Scale validation hops (6.3.1) | 100 vnodes ($5 \times 20$) | Semantic, Clustered | $nprobe \in \{1,2,3,5,8,12,20,40\}$; 50 queries | `hops_sweep_results_async_*` |
| Latency & baseline (6.4) | 5 nodes, Compose | Semantic, Clustered, Mono | $nprobe \in [1, 80]$; 50 queries | `results_async_*` (monolithic) |
| Dynamic node join (6.5) | $5 \to 6$ nodes, Compose | Semantic, Clustered | $nprobe = 2$; 50 queries; migration counts | `node_join_results_async_*` |
| Single node failure (6.7) | 5 nodes, Compose | Semantic, Clustered | $nprobe = 2$; 1 random kill; baseline/transient/healed | `fault_tolerance_results_async_*` |
| Sparse correlated failure (6.8.1) | 5 nodes, Compose | Semantic, Clustered | $nprobe = 5$; $RF+1 = 3$ contiguous kills | `disaster_results_async_*` |
| Dense correlated failure (6.8.2) | 100 vnodes ($5 \times 20$) | Semantic, Clustered | $nprobe \in \{1,5,20\}$; hot-node kill; 500 uniform queries | `disaster_results_vnode_async_*` |
| Doomed scenario worst-case (6.8.3) | 100 vnodes ($5 \times 20$) | Semantic, Clustered | $nprobe \in [1, 640]$; 3 kills; 8 doomed queries | `doomed_scenario_results_async_*` |
| Scale of c ladder (6.8.4) | 5, 25, 100, 275 vnodes | Semantic, Clustered | $nprobe = 5$; $RF+1 = 3$ kills; 500 Zipf queries | `ladder/prod.{semantic,clustered}.N*` |

## Usage Runbook

```bash
# 1. 5-node full-corpus suite (Sections 6.2, 6.3, 6.4, 6.5, 6.7, 6.8.1) + plot generation
./run_all_benchmarks_fullcorpus.sh

# 2. Headline routing hops at scale (Section 6.3.1, Figure 6.2)
./run_hops_scale_async.sh 20 30    # 5 x 20 = 100-node virtual ring

# 3. Dense-ring hot-node disaster distribution (Section 6.8.2, Figure 6.6)
./run_vnode_disaster.sh 20 30      # 100 vnodes, 500 uniform queries

# 4. Worst-case doomed queries (Section 6.8.3, Figure 6.7)
./run_doomed_scenario.sh 20 30     # 100 vnodes, high nprobe sweep up to 640

# 5. The scale of c ladder (Section 6.8.4, Table 6.12, Figure 6.8)
./run_disaster_ladder.sh           # runs rungs across N=5, 25, 100, 275

# 6. Regenerate ALL thesis figures (Figures 6.1 to 6.8) from saved results
python3 -m src.benchmarks.containerized.plot
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
