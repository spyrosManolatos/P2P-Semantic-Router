# Interactive Query Gateway & Web Portal (Thesis Section 5.8)

This document describes the design, API, and usage of the **Interactive Query Gateway** (`src/api/main.py`), formally presented in **Chapter 5, Section 5.8 («Γραφικό Περιβάλλον Επίδειξης: Η Διαδικτυακή Πύλη»)** of the thesis.

---

## 🎯 Purpose & Design Rationale

While Chapter 6 quantitatively benchmarks routing hops, latency, and recall across thousands of queries, the **Web Portal** provides **qualitative, per-query observability**:

1. **Random Entry Peer Simulation:** In a real P2P deployment, user requests enter via arbitrary peers. The gateway simulates this by selecting a random peer from the ring (or allowing the user to pick one).
2. **Topological Chord Ring Visualization:** Renders an interactive SVG of the circular $2^{160}$ identifier space:
   - **Nodes (blue dots):** Placed at their exact coordinate $\text{SHA-1}(\text{address}) \bmod 2^{m}$.
   - **Entry Node (highlighted blue ring):** The peer currently handling query vectorization and coordination.
   - **Probed Clusters (orange diamonds):** Placed at their exact coordinates $h(c) = c \cdot \lfloor 2^m / K \rfloor \bmod 2^m$.
   - **Routing Vectors (dashed lines):** Showing finger-table jumps from the entry node to the responsible primary peer and subsequent adjacent hops.
3. **Routing Decision & Finger Table Inspection:** Displays the exact finger table transitions and explains *why* a lookup took $h$ hops.
4. **Ranked Semantic Search Results:** Displays the retrieved courses with cosine similarity scores, course titles, categories, and descriptions.

> **Design Principle (Thesis Section 5.8):** The gateway is implemented as a self-contained, backend-only FastAPI service (`src/api/main.py`). It serves both the JSON REST API and the embedded single-page visualization (`GET /`). No external frontend framework, build tools, or Node.js runtime are required.

---

## 🚀 Quick Start Runbook

### 1. Start the Cluster with Gateway

Navigate to the containerized stack and launch the services:

```bash
# Main Architecture: Async Semantic Router (Port 8080)
cd containerized_environment/async_semantic_router
docker compose up -d

# (Optional) Baseline: Async Clustered DHT (Port 8081)
cd ../async_clustered_dht
docker compose up -d
```

### 2. Inject Data into the Ring

Because node storage is in-memory, the ring starts empty. Use `inject_data.py` to route courses into the active ring:

```bash
# For Async Semantic Router (inject 500 courses from the Kaggle dataset):
docker compose exec async-gateway python src/scripts/inject_data.py \
    --node async-bootstrap-node:5000 --limit 500 --transport json

# For Async Clustered DHT:
docker compose exec async-clustered-gateway python src/scripts/inject_data.py \
    --node async-clustered-bootstrap:5000 --limit 500 --transport json
```

*Note: `--limit 0` injects the entire 98,104-course Kaggle corpus.*

### 3. Open in Browser

- **Semantic Router Gateway:** [http://localhost:8080](http://localhost:8080)
- **Clustered DHT Gateway:** [http://localhost:8081](http://localhost:8081)
- **Interactive OpenAPI/Swagger Docs:** [http://localhost:8080/docs](http://localhost:8080/docs)

---

## 🔬 Live Comparison: The Core Thesis Result

To visually demonstrate why the Semantic Router decouples recall from routing cost:

1. Open `http://localhost:8080` (Semantic Router) and `http://localhost:8081` (Clustered DHT) side by side.
2. Enter the exact same query (e.g. `python for data science`) with `nprobe = 3`.
3. **Observe the Ring Diagram:**
   - **Semantic Router (`:8080`):** The 3 target clusters form a **contiguous arc** on the ring. The entry node performs 1 Chord lookup ($O(\log N)$) to reach the primary cluster, followed by immediate $O(1)$ ring steps to adjacent neighbors.
   - **Clustered DHT (`:8081`):** SHA-1 hashing scatters the 3 clusters to **completely random, opposing sides** of the ring. The node is forced to execute 3 separate multi-hop Chord traversals ($O(nprobe \cdot \log N)$).

---

## 📡 REST API Reference

The gateway exposes REST endpoints for programmatic access:

| Endpoint | Method | Description | Example Payload / Params |
|---|---|---|---|
| `/` | `GET` | Interactive browser dashboard | `text/html` |
| `/query` | `POST` | Execute similarity search & return full routing trace | `{"query": "machine learning", "nprobe": 2}` |
| `/nodes` | `GET` | Return live ring membership and ring positions | Returns JSON list of online peers |
| `/docs` | `GET` | Interactive OpenAPI Swagger UI | Documentation |

### Example `POST /query` Response

```json
{
  "query": "python for data science",
  "entry_node": "async-node-2:5000",
  "nprobe": 2,
  "hops": 1.0,
  "latency_ms": 312.4,
  "routing": {
    "primary_cluster": 45,
    "primary_owner": "async-bootstrap-node:5000",
    "clusters": [
      {"cluster_id": 45, "ring_pos_pct": 56.25, "owner": "async-bootstrap-node:5000"},
      {"cluster_id": 46, "ring_pos_pct": 57.50, "owner": "async-bootstrap-node:5000"}
    ]
  },
  "results": [
    {
      "course_title": "Python for Data Science Bootcamp",
      "similarity": 0.892,
      "category": "Development"
    }
  ]
}
```

---

## 🛑 Tear Down

```bash
cd containerized_environment/async_semantic_router && docker compose down
cd ../async_clustered_dht && docker compose down
```
