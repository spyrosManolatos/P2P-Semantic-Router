# Decentralized Vector Database (P2P Semantic Router)

This project is a Proof of Concept (v1.0) for a highly scalable, decentralized Vector Database. It marries the robust peer-to-peer architecture of a **Chord Distributed Hash Table (DHT)** with the mathematical precision of **Semantic Vector Search (KNN)**.

Unlike traditional categorical DHTs that rely on random SHA-1 hashing, this architecture mathematically maps K-Means semantic clusters across the circular ring. This ensures that semantically similar data is hosted in identical or adjacent network regions, enabling powerful distributed fanout queries.

## 📂 Directory Layout

```text
├── data/                      # Database and results storage
│   ├── benchmarks/            # Evaluation results and documentation
│   │   ├── plots/             # Rendered evaluation charts (PNG)
│   │   ├── results/           # Raw collected metric data (JSON)
│   │   └── README.md          # Benchmark results analysis report
│   ├── models/                # Trained K-Means centroids and TF-IDF artifacts
│   └── raw/                   # Udemy Kaggle CSV dataset and normalized JSON payloads
├── src/                       # Database source code
│   ├── architectures/         # Database topologies and simulations
│   │   ├── monolithic_linear/ # Centralized exhaustive KNN baseline
│   │   ├── standard_dht/      # Standard random-hash Chord DHT ring
│   │   ├── clustered_dht/     # Hashed K-Means clusters DHT ring
│   │   └── semantic_router/   # Mapped Agglomerative semantic Chord ring (Proposed)
│   ├── benchmarks/            # Scalability, fault-tolerance, and load-balancing benchmarks
│   ├── core/                  # Decoupled config loader and shared core utilities
│   ├── ml/                    # ML clustering and vocabulary training pipeline
│   └── scripts/               # Kaggle csv normalizer and synthetic course generators
├── config.yaml                # Decoupled network and database parameters file
└── README.md                  # Main project overview and run instructions
```

## 🏗️ Approaches Implemented

This repository implements and compares four different architectural approaches to Vector Search:

1. **Monolithic Linear Search (`src/architectures/monolithic_linear/`):** A centralized baseline that performs an exhaustive linear scan (exact KNN) over all vectors. Provides perfect recall but scales poorly as $O(N)$.
2. **Standard DHT (`src/architectures/standard_dht/`):** A standard Chord DHT implementation where vectors are distributed using random SHA-1 hashing. Provides highly scalable storage, but similarity queries are inefficient as they require broadcasting to all nodes because semantic locality is lost.
3. **Clustered DHT (`src/architectures/clustered_dht/`):** A hybrid baseline where data is grouped via K-Means, but the clusters are placed on the ring using random SHA-1 hashing rather than semantic geometric mapping. 
4. **P2P Semantic Router (`src/architectures/semantic_router/`):** The novel approach where the DHT ring is mathematically partitioned using K-Means centroids. Data is routed based on semantic similarity rather than random hashes, allowing for $O(1)$ routing to the correct cluster and distributed `nprobe` fanout for similarity searches.

---

## 🔑 Key Architectural Features

1. **Semantic Centroid Routing:** Nodes automatically vectorize raw text using TF-IDF and route the data to the correct cluster ID on the DHT ring.
2. **True Vector Embeddings:** Text is vectorized exactly _once_ during insertion (`PUT`), avoiding heavy $O(N)$ text-processing bottlenecks during queries.
3. **`nprobe` Distributed Fanout:** Similarity queries (`GET`) can seamlessly branch out across multiple mathematical clusters simultaneously to merge results, allowing a dynamic trade-off between speed and recall.
4. **Active Replica Load Balancing:** Solves the notorious "Hot Spot" CPU problem. If a node detects high query load, it mathematically delegates the read queries to its replica node, doubling the read capacity of the network without any data migration!
5. **Self-Healing Fault Tolerance:** Standard Chord stabilization protocols ensure that if a Primary node crashes, the Replica node instantly promotes its backup data to Primary.

---

## 🚀 Setup Instructions

This project requires a standard Python environment (Python 3.8+ recommended).

### 1. Create a Virtual Environment (Recommended)

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies

```bash
pip install -r requirements.txt
```

### 3. Configuration

The system parameters are fully decoupled from the code. You must create a local configuration file before running anything:

```bash
cp config.yaml.example config.yaml
```

_(You can open `config.yaml` to modify the number of nodes, replication factor (RF), ports, and dataset paths)._

---

## 🧠 Dataset Creation & ML Training Pipeline

Before running any simulations, the nodes need mathematical centroids to perform semantic routing. The dataset and models will be cleanly saved to the `data/` directory.

**Step 1: Generate the Raw Dataset**
Generate synthetic courses to populate `data/raw/data.json`. The number of courses is defined in `config.yaml` (`num_courses`).
```bash
python3 src/scripts/generate_synthetic_data.py
```

**Step 2: Train the Semantic K-Means Centroids**
This script parses the raw dataset, builds a TF-IDF vocabulary, and trains the semantic clusters. The resulting `centroids.json` is saved in `data/models/` so the DHT nodes can use it for $O(1)$ routing.
```bash
python3 src/ml/train_centroids.py
```

---

## 🧪 Running the Simulations

To mathematically prove the architecture, the project includes isolated simulation scripts for each architecture. These scripts will read `config.yaml`, spin up a local P2P network (e.g., 10 nodes), inject the dataset, execute tests, and gracefully shut down.

Run these commands from the root directory to test the **Semantic Router**:

**Simulation 1: Semantic Routing & KNN**
Proves the non-random ring mapping and the distributed Cosine Similarity top-5 ranking.
```bash
python3 src/architectures/semantic_router/simulations/01_semantic_routing.py
```

**Simulation 2: Fault Tolerance**
Proves standard DHT self-healing. Kills a node abruptly and verifies that the successor recovers the ring.
```bash
python3 src/architectures/semantic_router/simulations/02_node_failure.py
```

**Simulation 3: Active Replica CPU Delegation**
Proves the hot-spot load balancer. Blasts a specific cluster with rapid-fire queries and verifies that the Primary node successfully delegates the traffic to its successor.
```bash
python3 src/architectures/semantic_router/simulations/03_load_balancing.py
```

**Simulation 4: Dynamic Node Join & Replication**
Proves the self-healing and data migration of a new node joining an existing network, correctly inheriting the network's Replication Factor (RF) via Bootstrap discovery.
```bash
python3 src/architectures/semantic_router/simulations/04_node_join.py
```

_(You can run identical simulation scripts located inside the `src/architectures/clustered_dht/simulations/` and `src/architectures/standard_dht/simulations/` directories to compare their outputs.)_

---

## 📊 Benchmarks & Results

To evaluate the empirical trade-offs of the system, this project includes a consolidated benchmarking suite (`src/benchmarks/evaluate.py`) that tests the architectures across various metrics.

### Running the Evaluation Suite
You can execute the benchmarks for specific components or run the entire suite:

*   **Run all benchmarks end-to-end (Recommended):**
    ```bash
    python3 src/benchmarks/evaluate.py --mode all
    ```
*   **Run specific benchmark modes:**
    - **Scaling:** Multi-architecture comparison (Standard DHT, Clustered DHT, Semantic Router) at a scale of 2,000 courses.
      ```bash
      python3 src/benchmarks/evaluate.py --mode scale --dataset_size 2000
      ```
    - **Fault Tolerance:** Evaluation under node crashes (0% to 33.3% failures) with replication factor `RF=3`.
      ```bash
      python3 src/benchmarks/evaluate.py --mode fault --replication_factor 3
      ```
    - **Node Join:** Data migration and ring healing evaluation.
      ```bash
      python3 src/benchmarks/evaluate.py --mode join --replication_factor 3
      ```
    - **Load Balancing:** Average latency scaling under concurrent workloads (with vs. without delegation).
      ```bash
      python3 src/benchmarks/evaluate.py --mode load --replication_factor 3
      ```

### Generating Visualization Charts
After running the evaluations, generate all metrics curves and comparison plots by running:
```bash
python3 src/benchmarks/plot.py
```

### Metrics Measured:
- **Search Recall:** Accuracy against a monolithic exact-KNN baseline.
- **Network Hops:** Average routing hops and the impact of target node deduplication.
- **End-to-End Latency:** Latency scaling advantages under single and concurrent workloads.
- **Healing & Migration Speed:** Duration (in seconds) for rings to stabilize and transfer replica data over RPC.

**All raw collected metrics are saved in `data/benchmarks/results/` and generated visualization charts are stored in the [`data/benchmarks/plots/`](data/benchmarks/README.md) directory.** Please view the README in [`data/benchmarks/README.md`](data/benchmarks/README.md) for a full analysis of the findings!

---

## 🔮 Future Work

With the core architectures, dynamic self-healing, replication data migration, and active replica load-balancing fully benchmarked, future work will focus on scaling the deployment to production-grade distributed environments:

1. **Containerized Network Emulation (Real Latency & Bandwidth Constraints):**
   - Moving from `localhost` loopback socket configurations to dedicated Docker/Kubernetes container deployments.
   - Introducing real physical network propagation latency (e.g., 5ms to 50ms) across regions to evaluate the overhead of multi-hop Chord routing queries and background stabilization.

2. **Multi-Core Hardware Isolation (GIL Workload Optimization):**
   - Setting explicit CPU and memory resource constraints (limits/requests) per containerized node.
   - Measuring concurrent throughput scaling without local Python GIL thread-scheduling bottlenecks to prove true linear throughput scaling of active delegation.

3. **Network Chaos Engineering & Unclean Crashes:**
   - Utilizing tools like Chaos Mesh to inject packet drops, random packet delay (jitter), and split-brain network partitions to evaluate the robustness of the Chord ring stabilization protocols under adversarial network conditions.

4. **Massive Scale-Out Evaluations:**
   - Scaling deployments to 1,000+ nodes to test high-dimensional vector partitioning and confirm $O(\log N)$ network routing hops at a true enterprise scale.
