# Architecture Pipeline: P2P Semantic Router

This document outlines the four-phase system architecture and execution pipeline of the **P2P Semantic Router**, as formally presented in **Chapter 3 ("Ανάλυση και Σχεδίαση")** of the thesis.

---

## 🏛️ System Architecture Overview

The system operates across **three logical planes** (Thesis Section 3.2):

1. **Index Plane (Επίπεδο Ευρετηρίου):** Encapsulates the immutable machine learning artifact (`centroids.json`). It provides local text vectorization and centroid assignment, guaranteeing deterministic semantic locality without any central index node.
2. **Routing Plane (Επίπεδο Δρομολόγησης):** Implements the decentralized Chord DHT overlay (finger tables, successor lists, stabilization). It resolves cluster positions to responsible network peers in $O(\log N)$ steps.
3. **Data Plane (Επίπεδο Δεδομένων):** Handles in-memory storage, synchronous/asynchronous replication, and local vector dot-product scoring.

---

## Phase 1: Offline ML Training — The Topology Builder (Thesis 3.3)

Before network bootstrapping, global semantic knowledge is computed once offline and condensed into an immutable, self-describing artifact (`centroids.json`):

1. **Corpus Text Construction (Step 1):** Concatenates `title`, `category`, and `description` (combining headline + objectives) into a rich semantic string for each document.
2. **TF-IDF Vectorization (Step 2):** Extracts a vocabulary of $d = 1,000$ terms (stop-words removed) and produces $L_2$-normalized sparse vectors $v \in \mathbb{R}^d$. As proved in **Appendix A**, for $L_2$-normalized vectors, Euclidean distance and Cosine similarity yield identical rankings.
3. **Voronoi Partitioning via K-Means (Step 3):** Clusters the document space into $K$ Voronoi cells ($K=5,500$ for the full 98,104-course corpus, following the empirical rule $K_{IVF} / \sqrt{n} \approx 17.6$).
4. **1D Semantic Ordering via Agglomerative Clustering (Step 4):** Performs hierarchical clustering (Average Linkage / UPGMA) strictly on the $K$ centroids using Euclidean distance. The leaves of the resulting dendrogram are flattened left-to-right to establish an optimal 1D semantic continuum.
5. **Cluster Identifier Relabeling (Step 5):** Centroids are reassigned sequential integer IDs $c \in \{0, 1, \dots, K-1\}$ according to the leaf order. Consequently, **adjacent integer IDs represent semantically adjacent clusters**.
6. **Artifact Export:** Saved to `centroids.json` containing $K$, $d$, vocabulary mapping, and the $K \times d$ ordered centroid matrix. Every peer loads an identical copy at boot.

---

## Phase 2: Network Bootstrapping & Semantic Ring Mapping (Thesis 3.4)

The DHT overlay is established with a deliberate **placement asymmetry**:

- **Random Node Placement:** Nodes hash their announced network address using SHA-1:
  $$\text{Node ID} = \text{SHA-1}(\text{address}) \bmod 2^m \quad (m=160)$$
  This guarantees uniform node distribution and balanced responsibility arcs.
- **Deterministic Linear Cluster Placement:** Clusters are **not hashed**. Instead, each cluster ID $c \in \{0, \dots, K-1\}$ is mapped deterministically across the ring:
  $$h(c) = c \cdot \left\lfloor \frac{2^m}{K} \right\rfloor \bmod 2^m$$
  **Consequence:** Semantically adjacent clusters occupy topologically adjacent arcs on the ring. Any peer can compute the exact ring coordinate of any cluster in $O(1)$ without metadata lookups.

---

## Phase 3: Data Ingestion — The `PUT` Pipeline (Thesis 3.5)

Data ingestion follows the **write-read decoupling principle**: expensive operations are performed once at insertion time so queries remain blisteringly fast.

1. **Vectorize Once & Embed (Stage 1):** The entry peer vectorizes incoming course text against the pre-loaded vocabulary ($O(d)$) and embeds the normalized vector directly into the JSON record payload.
2. **Nearest Centroid Assignment (Stage 2):** Computes Euclidean distance across the $K$ centroids ($O(K \cdot d)$) to find the primary cluster $c^* = \arg\min_c \|x - \mu_c\|_2$.
3. **Chord Routing to Primary Peer (Stage 3):** Calculates $h(c^*)$ and routes the payload via Chord finger tables to the responsible successor peer in $O(\log N)$ hops.
4. **Synchronous Replication (Stage 4):** The primary peer stores the course locally in its cluster partition and immediately forwards a replica to its immediate successor ($RF=2$), providing instant single-failure tolerance.

---

## Phase 4: Semantic Similarity Search — The `GET` Pipeline (Thesis 3.6)

Query execution achieves predictable $O(\log N + nprobe)$ routing cost, completely independent of vector dimensionality $d$:

1. **Entry Vectorization (Stage 1):** Any arbitrary entry peer vectorizes the free-text query ($O(K \cdot d)$ local computation).
2. **Target Cluster Resolution via Ring Adjacency (Stage 2):** Identifies the nearest centroid $c^*$. When $nprobe > 1$, additional clusters are selected by alternating ring adjacency:
   $$\{c^*, c^*+1, c^*-1, c^*+2, \dots\}$$
   Because IDs are topologically ordered, these target clusters form a contiguous ring segment.
3. **Two-Level Routing (Stage 3):**
   - **Level 1 — Single Chord Lookup:** Routes to the primary peer owning $c^*$ via finger tables in $O(\log N)$ hops.
   - **Level 2 — Direct $O(1)$ Adjacent Traversal:** The primary peer satisfies all target clusters in its arc and forwards the remaining request directly to its immediate successor/predecessor (`retrieve_local_adjacent`), completely bypassing finger tables. Each additional peer along the arc costs exactly **1 hop**.
4. **Vector Dot-Product Ranking (Stage 4):** Candidate courses returned from the target clusters already carry their pre-computed $L_2$-normalized vectors. The entry peer scores candidates using single-instruction vector dot products (Cosine Similarity), sorts in descending order, and returns Top-$\kappa$ matches.

$$\mathbf{T_{\text{GET}} = O(\log N + nprobe)}$$

---

## Cross-Cutting: Self-Healing & Dynamic Join (Thesis 3.7)

- **Stabilization Loop:** Periodic background cycles (`stabilize()`, `notify()`, `fix_fingers()`, `check_predecessor()`) reconcile ring topology asynchronously.
- **Successor List ($r \ge RF+1$):** Guarantees ring connectivity under concurrent node failures (**Appendix B**).
- **Instant Replica Promotion:** When a primary peer fails, its successor already hosts the replica and promotes it to primary instantly with zero data loss.
- **Dynamic Node Join & Migration (`claim_and_migrate_data`):** New nodes split their successor's primary arc and migrate relevant data seamlessly.

