# Toy Example: Node Join and Data Migration

This document illustrates the step-by-step process of a new node joining our existing 128-key space P2P semantic ring, including how the network transfers data safely.

---

## 1. Initial State

Our initial ring consists of 3 nodes:
* **Node A:** ID = **20**
* **Node B:** ID = **50**
* **Node C:** ID = **80**

The existing semantic clusters are mapped and stored as follows:
* **Cluster 0 (0)** -> Stored at **Node A** (Range: `[81, 20]`)
* **Cluster 1 (25)** -> Stored at **Node B** (Range: `[21, 50]`)
* **Cluster 2 (51)** -> Stored at **Node C** (Range: `[51, 80]`)
* **Cluster 3 (76)** -> Stored at **Node C** (Range: `[51, 80]`)
* **Cluster 4 (102)** -> Stored at **Node A** (Range: `[81, 20]`)

---

## 2. A New Node Arrives

A new machine boots up, hashes its IP address, and gets an ID.
* **Node D:** ID = **35**

Node D wants to join the network. It knows the IP address of at least one existing node (the "bootstrap" node), for example, Node A (20).

### Step 1: Finding the Successor
Node D asks Node A: *"Who is the successor of my ID (35)?"*
* Node A checks its finger table.
* Node A sees that key **35** falls in the interval `[21, 50]`, which points to Node B (50).
* Node A replies: *"Your successor is Node B (50)."*

### Step 2: Inserting into the Ring Topology
Node D inserts itself between Node A and Node B.
* Node D sets its successor to **Node B (50)**.
* Node B updates its predecessor to **Node D (35)**.
* Node A updates its successor to **Node D (35)**.

**New Ring Topology:**
`[0] -> Node A (20) -> Node D (35) -> Node B (50) -> Node C (80) -> [128]`

---

## 3. Data Migration (Transferring Clusters)

When Node D joins, it splits Node B's territory.
* **Old Node B Range:** `[21, 50]`
* **New Node D Range:** `[21, 35]`
* **New Node B Range:** `[36, 50]`

Because the territory changed, Node B must hand over any clusters that now belong to Node D. Node B scans its local storage:
* Does **Cluster 1 (Key 25)** belong to Node D? **Yes**, because $21 \le 25 \le 35$.

Node B packages all courses mapped to Cluster 1 and sends them over the network to Node D. Once Node D acknowledges receipt, Node B deletes Cluster 1 from its local memory.

---

## 4. Updating Finger Tables (Stabilization)

As the network stabilizes, nodes periodically update their finger tables to reflect the new topology. 

### Node A (20) Updated Finger Table
Notice how Node A's first few fingers now point to the new Node D instead of Node B!
* Key Range `[21, 35]` (Fingers 1 to 4) -> **Node D (35)** *(Updated!)*
* Key Range `[36, 50]` (Finger 5) -> **Node B (50)**
* Key Range `[51, 80]` (Finger 6) -> **Node C (80)**
* Key Range `[81, 20]` (Finger 7) -> **Node A (20)** (wraps around)

### Node D (35) Initial Finger Table
Node D calculates its own finger table (offsets of +1, +2, +4, +8, +16, +32, +64):
* Key Range `[36, 50]` (Fingers 1 to 4) -> **Node B (50)**
* Key Range `[51, 80]` (Fingers 5 & 6) -> **Node C (80)**
* Key Range `[81, 35]` (Finger 7) -> **Node A (20)** (wraps around)

*(Note: Node B and Node C's finger tables do not change in this specific scenario, because none of their $2^{i-1}$ offsets land in the `[21, 35]` interval.)*
