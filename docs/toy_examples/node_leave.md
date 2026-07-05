# Toy Example: Abrupt Node Failure and Replica Recovery

This document illustrates the step-by-step process of what happens when a node suddenly crashes in our 128-key space P2P semantic ring, and how the network uses stabilization and replication to prevent data loss.

---

## 1. Initial State (4-Node Ring with Replicas)

We start with the 4-node ring that was formed after Node D joined.
* **Node A:** ID = **20**
* **Node D:** ID = **35**
* **Node B:** ID = **50**
* **Node C:** ID = **80**

**Current Topology:**
`[0] -> Node A (20) -> Node D (35) -> Node B (50) -> Node C (80) -> [128]`

**Primary Data Ownership:**
Node D (35) is currently responsible for the key range `[21, 35]`. 
Because $21 \le 25 \le 35$, Node D is currently storing the **Primary copy** of **Cluster 1 (Key 25)**.

**Replica Data Ownership:**
In our implementation, every node continuously syncs its primary data to its successor as a backup.
* Node D's successor is Node B (50).
* Therefore, **Node B holds a Replica copy of Cluster 1**.

---

## 2. A Node Crashes Abruptly

Node D (35) suffers a sudden power outage (simulated by calling `node.stop()`). It drops offline instantly without notifying anyone.

### Step 1: Failure Detection and Topology Healing
The remaining nodes run periodic background threads (`stabilize()` and `check_predecessor()`) to monitor network health.

1. **Node A Detects Failure:** Node A tries to ping its successor (Node D). The RPC call times out!
2. **Finding the Next Alive Node:** Node A drops Node D and scans its finger table for the next responsive node. It successfully pings **Node B (50)**.
3. **Updating Pointers:** 
   * Node A updates its successor to **Node B (50)**.
   * Node B's `check_predecessor()` detects that Node D is dead and drops its predecessor pointer.
   * On the next `stabilize()` loop, Node A introduces itself to Node B, and Node B sets its predecessor to **Node A (20)**.

**New Ring Topology (Healed):**
`[0] -> Node A (20) -> Node B (50) -> Node C (80) -> [128]`
*(We are back to the original 3 nodes!)*

### Step 2: Data Recovery via Replication
When Node D crashed, the primary copy of **Cluster 1 (25)** was lost. 

However, because the ring healed, Node B (50)'s primary responsibility range automatically expanded from `[36, 50]` to `[21, 50]` (since its predecessor is now Node A).
* Does Cluster 1 (25) fall in `[21, 50]`? **Yes.**
* Because Node B already held the **Replica** of Cluster 1 in its local storage, that replica is automatically **"promoted"** to the primary copy!
* **No data is lost.** The network seamlessly recovers.

---

## 3. Updating Finger Tables (Stabilization)

As the stabilization protocol continues running, nodes fix their broken routing tables using `fix_fingers()`.

### Node A (20) Updated Finger Table
Previously, Node A's first few fingers pointed to Node D. Now that Node D is unreachable, the algorithm recalculates the successors and collapses the interval back to Node B.

**Before (When D was alive):**
* Key Range `[21, 35]` -> Node D (35)
* Key Range `[36, 50]` -> Node B (50)

**After (Healed):**
* Key Range `[21, 50]` (Fingers 1 to 5) -> **Node B (50)** *(Fixed!)*
* Key Range `[51, 80]` (Finger 6) -> **Node C (80)**
* Key Range `[81, 20]` (Finger 7) -> **Node A (20)** (wraps around)

*(Once healed, Node B will automatically start syncing its new primary data, including Cluster 1, forward to its own successor, Node C, to maintain redundancy.)*
