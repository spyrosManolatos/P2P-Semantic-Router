# Decentralized Vector Database (P2P Semantic Router)

This project is a Proof of Concept (v1.0) for a highly scalable, decentralized Vector Database. It marries the robust peer-to-peer architecture of a **Chord Distributed Hash Table (DHT)** with the mathematical precision of **Semantic Vector Search (KNN)**.

Unlike traditional categorical DHTs that rely on random SHA-1 hashing, this architecture mathematically maps K-Means semantic clusters across the circular ring. This ensures that semantically similar data is hosted in identical or adjacent network regions, enabling powerful distributed fanout queries.

## Key Architectural Features

1. **Semantic Centroid Routing:** Nodes automatically vectorize raw text using TF-IDF and route the data to the correct cluster ID on the DHT ring.
2. **True Vector Embeddings:** Text is vectorized exactly *once* during insertion (`PUT`), avoiding heavy $O(N)$ text-processing bottlenecks during queries.
3. **`nprobe` Distributed Fanout:** Similarity queries (`GET`) can seamlessly branch out across multiple mathematical clusters simultaneously to merge results, allowing a dynamic trade-off between speed and recall.
4. **Active Replica Load Balancing:** Solves the notorious "Hot Spot" CPU problem. If a node detects high query load, it mathematically delegates the read queries to its replica node, doubling the read capacity of the network without any data migration!
5. **Self-Healing Fault Tolerance:** Standard Chord stabilization protocols ensure that if a Primary node crashes, the Replica node instantly promotes its backup data to Primary.

---

## 🚀 Setup Instructions

This project requires a standard Python environment. 

### 1. Create a Virtual Environment (Recommended)
```bash
python3 -m venv .venv
source .venv/bin/activate
```

### 2. Install Dependencies
```bash
pip install -r requirements.txt
```

---

## 🧪 Running the Simulations

To mathematically prove the architecture, the project includes three isolated simulation scripts. These scripts will automatically spin up a local 4-node network, inject 100 synthetic academic courses, execute the tests, and shut down.

Run these commands from the root directory:

**Simulation 1: Semantic Routing & KNN**
Proves the non-random ring mapping and the $O(1)$ Cosine Similarity top-5 ranking.
```bash
python test/simulations/01_semantic_routing.py
```

**Simulation 2: Fault Tolerance**
Proves standard DHT self-healing. Kills a node and verifies that the replica data is successfully promoted and served.
```bash
python test/simulations/02_node_failure.py
```

**Simulation 3: Active Replica CPU Delegation**
Proves the hot-spot load balancer. Blasts a specific cluster with rapid-fire queries and verifies that the Primary node successfully delegates the traffic to its successor.
```bash
python test/simulations/03_load_balancing.py
```

**Simulation 4: Node Join**
Proves the self-healing and load balancing of a new node joining an existing network.
```bash
python test/simulations/04_node_join.py
```
---

## 🧠 ML Training Pipeline

The global centroid tables (`centroids.json`) are pre-trained. If you modify the synthetic dataset in `test/synth_data/data.json`, you must recalculate the mathematical clusters before running the simulations.

To retrain the clusters (Default $K=5$):
```bash
python test/synth_data/train_centroids.py
```
