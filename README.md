# Decentralized Vector Database (P2P Semantic Router)

This project is a Proof of Concept (v1.0) for a highly scalable, decentralized Vector Database. It marries the robust peer-to-peer architecture of a **Chord Distributed Hash Table (DHT)** with the mathematical precision of **Semantic Vector Search (KNN)**.

Unlike traditional categorical DHTs that rely on random SHA-1 hashing, this architecture mathematically maps K-Means semantic clusters across the circular ring. This ensures that semantically similar data is hosted in identical or adjacent network regions, enabling powerful distributed fanout queries.

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

## 🔮 Future Work

As the core architectures of the P2P Semantic Router and its baselines are established, future development will focus on rigorous evaluation and deployment realism:

1. **Comprehensive Benchmarking:**
   - **Recall:** Evaluating the accuracy of distributed similarity searches against the monolithic baseline.
   - **Network Hops:** Profiling the routing efficiency and the impact of the `nprobe` fanout mechanism.
   - **Latency:** Measuring end-to-end query resolution times under various network sizes.

3. **Containerization & Distributed Simulation:**
   - Packaging nodes into isolated Docker containers.
   - Using Docker Compose to orchestrate realistic network topologies, allowing for the simulation of network latency, partition events, and resource constraints in a true distributed environment.
