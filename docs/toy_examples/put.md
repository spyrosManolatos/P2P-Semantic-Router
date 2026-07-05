# Toy Example: Semantic Routing and Chord PUT Operation

This document provides a simplified representation of the peer-to-peer (P2P) semantic router ring topology, finger tables, and a step-by-step trace of a multi-hop routing operation.

---

## 1. Network Topology Specification

To calculate the binary finger tables cleanly, we scale down the Chord ring space from the standard 160-bit space to a circular scale of **0 to 128** (using `m = 7` bits, since $2^7 = 128$).

### Peer Node Placements
We spin up **3 Nodes** in our network. Their addresses are hashed, placing them at these coordinates:
* **Node A:** ID = **20**
* **Node B:** ID = **50**
* **Node C:** ID = **80**

The nodes form a circular ring clockwise:
`[0] -> Node A (20) -> Node B (50) -> Node C (80) -> [128] (wraps back to 0)`

---

## 2. Finger Tables (The Routing Tables)

In Chord, each node maintains a finger table with $m$ entries (where $m = 7$ in our case). 
The $i$-th entry of node $N$ points to the successor of $(N + 2^{i-1}) \pmod{128}$.

### Node A (20) Finger Table Intervals
* Key Range `[21, 50]` (Fingers 1 to 5) -> **Node B (50)**
* Key Range `[51, 80]` (Finger 6) -> **Node C (80)**
* Key Range `[81, 20]` (Finger 7) -> **Node A (20)** (wraps around)

### Node B (50) Finger Table Intervals
* Key Range `[51, 80]` (Fingers 1 to 5) -> **Node C (80)**
* Key Range `[81, 20]` (Fingers 6 & 7) -> **Node A (20)** (wraps around)

### Node C (80) Finger Table Intervals
* Key Range `[81, 20]` (Fingers 1 to 7) -> **Node A (20)** (wraps around)

---

## 3. Semantic Cluster Mapping (K = 5)

We divide the 128-key space into 5 segments (rounded to the nearest integer):
* **Cluster 0:** Anchor Coordinate = **0**
* **Cluster 1:** Anchor Coordinate = **25**
* **Cluster 2:** Anchor Coordinate = **51**
* **Cluster 3:** Anchor Coordinate = **76**
* **Cluster 4:** Anchor Coordinate = **102**

---

## 4. Execution Trace: Multi-Hop Routing (`PUT`)

Suppose a client contacts **Node C (80)** to store the following course:
```json
{
  "course_title": "Introduction to Databases",
  "category": "Computer Science"
}
```

### Step 1: Centroid Mapping (Machine Learning)
The entry node (Node C) maps the course to **Cluster 1** using vector distance calculations.
* **Target Cluster:** Cluster 1
* **Target Key (Coordinate):** **25** (Cluster 1's anchor coordinate)

### Step 2: Route from Node C (80) -> Node A (20) [Hop 1]
* Node C checks if key 25 is in its primary storage range (50, 80]. **No**.
* Node C checks if key 25 is between itself (80) and its successor Node A (20).
* Since $80 < 128 \pmod{128}$ and $0 \le 25 \le 20$, the range (80, 20] contains key 25.
* Node C forwards the lookup request to its successor: **Node A (20)**.

### Step 3: Route from Node A (20) -> Node B (50) [Hop 2]
* Node A receives the request for key 25.
* Node A checks if key 25 is in its primary range (80, 20]. **No** (25 > 20).
* Node A checks if key 25 is in its successor range (20, 50]. **Yes** (25 lies between 20 and 50).
* Node A forwards the request to its successor: **Node B (50)**.

### Step 4: Storage at Node B (50) [Destination]
* Node B receives the request.
* Node B checks if key 25 is in its primary range (20, 50]. **Yes**.
* Node B stores the course locally in its memory under cluster key `"1"`.

**Total Path taken:** Node C -> Node A -> Node B (2 Hops).

---

## Summary of Responsibilities on this Ring

Based on successor ranges, the nodes hold primary responsibility for:
* **Node A (20):** Primary for key range (80, 20] -> **Cluster 4** (102) and **Cluster 0** (0)
* **Node B (50):** Primary for key range (20, 50] -> **Cluster 1** (25)
* **Node C (80):** Primary for key range (50, 80] -> **Cluster 2** (51) and **Cluster 3** (76)
