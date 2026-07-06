# Novel Implementation: The P2P Semantic Router Pipeline

This document outlines the complete architectural pipeline of our novel Decentralized Vector Database, demonstrating the integration of Machine Learning (ML) clustering with Peer-to-Peer (P2P) Distributed Hash Table (DHT) networks.

---

## Phase 1: Offline ML Pre-Training (The Topology Builder)
Before the decentralized network boots up, we must mathematically define the semantic landscape.

1. **TF-IDF Vectorization**: Raw text (Title + Category + Description) from a large training dataset is converted into high-dimensional mathematical vectors.
2. **K-Means Clustering ($O(N)$)**: We run a highly efficient K-Means algorithm over the entire dataset to instantly isolate the $K$ semantic center points (centroids).
3. **Agglomerative Hierarchical Clustering ($O(1)$)**: To ensure *semantic locality* on a 1D network ring, we run Agglomerative Clustering strictly on the $K$ centroids. This builds a dendrogram (a tree). We flatten the leaves of this tree from left-to-right to establish a perfect 1D semantic sequence.
4. **Export**: The ordered centroids and the TF-IDF vocabulary are saved to `centroids.json`.

---

## Phase 2: Network Bootstrapping (The DHT Layer)
1. **Initialization**: The peers boot up, load the `centroids.json` into RAM, and form a Chord ring architecture.
2. **Non-Random Ring Mapping**: Unlike traditional DHTs that use randomized SHA-1 hashing, this architecture uses `get_cluster_hash()` to map the 1D Dendrogram sequence sequentially across the 160-bit ring. This guarantees that **semantically adjacent clusters (e.g., Cluster 0 and Cluster 1) sit physically next to each other on the network.**

---

## Phase 3: Data Insertion (`PUT`)
When a user uploads a new course to *any* peer in the network:

1. **Local Embedding**: The entry peer vectorizes the text using the pre-loaded vocabulary. Crucially, it **embeds the mathematical vector directly into the JSON data structure**.
2. **Centroid Proximity**: Using Euclidean distance, the peer determines which of the global centroids the new vector is closest to (e.g., Cluster 2).
3. **DHT Routing**: The peer calculates the physical location of Cluster 2 on the ring and traverses the finger tables to forward the JSON payload to the responsible Primary Peer.
4. **Active Replication**: The Primary Peer saves the data to its local disk and immediately forwards a backup copy to its mathematical successor for fault tolerance.

---

## Phase 4: Semantic Querying & KNN (`GET`)
When a user searches for a concept (e.g., "Machine Learning"):

1. **Query Vectorization**: The entry peer vectorizes the query text.
2. **Targeting**: The peer identifies the top `nprobe` closest centroids to the query vector. 
3. **Distributed Fanout**: The peer fires parallel network requests to fetch the data specifically belonging to those target clusters. *By targeting specific clusters instead of scanning entire peers, we avoid wasting CPU cycles on mathematically irrelevant data.*
4. **$O(1)$ Cosine Similarity Ranking**: Because the mathematical vectors were pre-embedded into the JSON during Phase 3, the entry peer skips text processing entirely. It performs an instant linear algebra dot-product (Cosine Similarity) across the returned courses, sorts them in descending order, and returns the absolute Top-5 matches to the user.
