import json
import hashlib
import socket
import asyncio
import time
import math
import os
import sys
import numpy as np
import httpx
from typing import List, Dict, Any, Optional

# Ensure src/ is in the python path to import core
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core import config_loader

_ip_cache: Dict[str, str] = {}


def _resolve_ip(host: str) -> str:
    """Resolves a container hostname to its real IP, cached per-process (the
    IP never changes during a container's lifetime). Ring IDs are hashed from
    the real IP rather than the hostname so that placement is identical
    regardless of naming convention (e.g. the "async-" transport-selecting
    prefix used by evaluate.py's rpc_client(), or per-architecture container
    name differences) -- every stack pins the same static IP per node role
    (see docker-compose.yml's ring_net), so node-1 hashes identically whether
    it's semantic, clustered, or standard, sync or async."""
    if host not in _ip_cache:
        try:
            _ip_cache[host] = socket.gethostbyname(host)
        except socket.gaierror:
            _ip_cache[host] = host
    return _ip_cache[host]


def in_half_open_range(val: int, start: int, end: int) -> bool:
    """Checks if val is in (start, end] on the circular ring."""
    if start == end:
        return True
    if start < end:
        return start < val <= end
    else:
        return val > start or val <= end

def in_open_range(val: int, start: int, end: int) -> bool:
    """Checks if val is in (start, end) on the circular ring."""
    if start == end:
        return val != start
    if start < end:
        return start < val < end
    else:
        return val > start or val < end

class ChordNode:
    """A node in the Chord distributed hash table, using an async FastAPI/httpx
    RPC layer instead of xmlrpc.server/xmlrpc.client. This is the same routing,
    replication and active-delegation logic as architectures.semantic_router.node,
    with one deliberate change: outbound peer calls share a single persistent,
    connection-pooled httpx.AsyncClient instead of opening a fresh blocking
    connection per XML-RPC call, and the server dispatches requests on an asyncio
    event loop instead of a thread-per-request model. This isolates whether the
    §6.7 load-balancing regression is caused by the RPC transport, not the
    delegation logic itself (which is unchanged)."""

    def __init__(self, ip: str, port: int, m: int = None, r: int = None, dataset: str = "kaggle"):
        self.config = config_loader.load_config()
        self.dataset = dataset

        self.ip = ip
        self.port = port
        self.address = f"{ip}:{port}"

        self.m = m if m is not None else self.config['dht']['hash_bits_m']
        self.rf = r if r is not None else self.config['dht']['replication_factor']
        # Successor-LIST length. Deliberately NOT tied to the data replication
        # factor: rf says how many copies of a document exist, r says how many
        # backup successors a node can fall through when peers die. The disaster
        # model kills RF+1 ADJACENT nodes -- one more than a list of length rf can
        # hold -- so with r = rf the list empties BY CONSTRUCTION and stabilize()
        # drops into the 160-finger rescue scan, which at N=550 never finished.
        # Chord sizes this as r ~ log2(N). Data replication is untouched: replicas
        # still go to self.successors[:self.rf] (see sync_replicas_to_successors).
        self.r = max(1, int(self.config['dht'].get('successor_list_size', self.rf)))
        self.stabilize_interval = self.config['dht']['stabilize_interval_sec']
        self.timeout = self.config['network']['timeout_sec']
        self.node_id = self.get_addr_hash(self.address)

        self.successors = [self.address]
        # Failure-detector hysteresis: a successor is only declared dead after
        # this many CONSECUTIVE missed pings, so a single ping that times out
        # because the target's event loop was momentarily busy (heavy under
        # 20 vnodes/container during bulk-load/replication) doesn't cause a
        # false removal -> successor-list churn -> finger non-convergence.
        self._succ_miss: Dict[str, int] = {}
        self._succ_miss_threshold = 3
        self.predecessor = None
        self.finger_table = [self.address] * self.m
        self.storage = {}  # Maps str(cluster_id) -> Dict[course_id, course_json]
        # Tracks whether primary storage changed since the last replica sync,
        # so the periodic tick can skip re-marshalling/re-sending the whole
        # primary set when nothing changed (see sync_replicas_to_successors).
        self._replica_sync_dirty = False

        self.shutdown_event = asyncio.Event()
        self._periodic_task = None
        self._uvicorn_server = None  # set externally by server.py

        # Persistent, connection-pooled async client shared by every outbound
        # RPC this node makes -- the deliberate transport change under test.
        self._http = httpx.AsyncClient(timeout=self.timeout)

        # Load balancing metrics

        # Load global centroid table
        self.k = 0
        self.n_features = 0
        self.vocabulary = {}
        self.centroids = []
        self._load_centroids()

    def _load_centroids(self):
        if self.dataset == "synthetic":
            path = self.config['storage']['centroids']['synthetic_path']
        else:
            path = self.config['storage']['centroids']['kaggle_dataset_path']

        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Centroid artifact missing: {path}. "
                f"Run 'python3 src/ml/train_centroids.py --dataset {self.dataset}' first."
            )

        global _ARTIFACT_CACHE
        try:
            _ARTIFACT_CACHE
        except NameError:
            _ARTIFACT_CACHE = {}
        if path not in _ARTIFACT_CACHE:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            centroids_np = np.asarray(data["centroids"], dtype=np.float32)
            _ARTIFACT_CACHE[path] = {
                "k": data["k"],
                "n_features": data["n_features"],
                "vocabulary": data["vocabulary"],
                "centroids": centroids_np,
                "centroid_norms2": (centroids_np * centroids_np).sum(axis=1),
            }
        art = _ARTIFACT_CACHE[path]
        self.k = art["k"]
        self.n_features = art["n_features"]
        self.vocabulary = art["vocabulary"]
        self.centroids = art["centroids"]
        self._centroid_norms2 = art["centroid_norms2"]

        if self.k <= 0 or len(self.centroids) != self.k:
            raise ValueError(
                f"Corrupt centroid artifact {path}: k={self.k} but "
                f"{len(self.centroids)} centroids present."
            )

        print(f"[{self.address}] Loaded artifact: k={self.k}, d={self.n_features}")

    def get_cluster_hash(self, cluster_id: int) -> int:
        """Maps a cluster ID linearly across the 0 to 2^m Chord ring to preserve locality."""
        if self.k <= 0: return 0
        chunk_size = (2 ** self.m) // self.k
        return (cluster_id * chunk_size) % (2 ** self.m)

    def _vectorize(self, text: str) -> List[float]:
        """Converts text to an L2 normalized TF vector."""
        tokens = text.lower().split()

        vec = [0.0] * self.n_features
        for token in tokens:
            if token in self.vocabulary:
                idx = self.vocabulary[token]
                vec[idx] += 1.0

        norm = math.sqrt(sum(v*v for v in vec))
        if norm > 0:
            vec = [v/norm for v in vec]
        return vec

    def _vectorize_and_find_centroids(self, text: str, nprobe: int = 1) -> List[int]:
        """Converts text to vector, calculates distance, returns top `nprobe` cluster IDs."""
        vec = self._vectorize(text)

        x = np.asarray(vec, dtype=np.float32)
        best_c_id = int((self._centroid_norms2 - 2.0 * (self.centroids @ x)).argmin())

        result = [best_c_id]
        radius = 1
        while len(result) < nprobe:
            result.append((best_c_id + radius) % self.k)
            if len(result) >= nprobe:
                break
            result.append((best_c_id - radius) % self.k)
            radius += 1

        return result

    @property
    def successor(self) -> str:
        return self.successors[0] if self.successors else self.address

    @property
    def successor_id(self) -> int:
        return self.get_addr_hash(self.successor)

    def get_hash(self, val: str) -> int:
        """Determines the plain SHA-1 hash of an arbitrary string (data keys,
        not peer addresses -- use get_addr_hash for those)."""
        if not val:
            return 0
        return int(hashlib.sha1(val.encode('utf-8')).hexdigest(), 16)

    def get_addr_hash(self, addr: str) -> int:
        """Determines the SHA-1 hash of a peer address's real IP (see
        _resolve_ip), not its hostname -- so ring placement doesn't depend on
        naming. Used for all node-identity/ring-topology hashing."""
        if not addr:
            return 0
        host, _, port = addr.partition(":")
        ip = _resolve_ip(host)
        canonical = f"{ip}:{port}" if port else ip
        return int(hashlib.sha1(canonical.encode('utf-8')).hexdigest(), 16)

    async def _rpc(self, addr: str, method: str, *args, **kwargs):
        """Calls `method` on the peer at `addr` over the shared, pooled httpx
        client (no per-call connection setup) instead of a fresh xmlrpc.client
        ServerProxy."""
        resp = await self._http.post(f"http://{addr}/rpc/{method}", json={"args": args, "kwargs": kwargs})
        try:
            payload = resp.json()
        except Exception:
            resp.raise_for_status()
            raise
        if "error" in payload:
            raise RuntimeError(payload["error"])
        return payload["result"]

    # --- RPC Exposed Methods ---

    def ping(self) -> bool:
        return True

    def get_replication_factor(self) -> int:
        return self.rf

    def set_replication_factor(self, rf: int) -> bool:
        """Runtime override of the replication factor. Used by the nprobe-recovery
        benchmark to force RF=0 (primary-only, no replica) for the un-replicated
        hotspot-failure scenario, without editing config or rebuilding the image."""
        self.rf = int(rf)
        return True

    def get_successor_list(self) -> List[str]:
        return self.successors

    def get_successor(self) -> str:
        return self.successor

    def get_predecessor(self) -> Optional[str]:
        return self.predecessor

    async def find_successor(self, id_str: str) -> str:
        id_val = int(id_str)
        if in_half_open_range(id_val, self.node_id, self.successor_id):
            return self.successor
        else:
            n0_addr = self.closest_preceding_node(id_val)
            if n0_addr == self.address:
                return self.successor
            try:
                return await self._rpc(n0_addr, "find_successor", str(id_val))
            except Exception:
                return self.successor

    async def find_successor_with_hops(self, id_str: str, hop_count: int = 0) -> List[Any]:
        id_val = int(id_str)
        if in_half_open_range(id_val, self.node_id, self.successor_id):
            return [self.successor, hop_count]
        else:
            n0_addr = self.closest_preceding_node(id_val)
            if n0_addr == self.address:
                return [self.successor, hop_count]
            try:
                return await self._rpc(n0_addr, "find_successor_with_hops", str(id_val), hop_count + 1)
            except Exception:
                return [self.successor, hop_count]

    async def find_successor_traced(self, id_str: str, hop_count: int = 0, path: Optional[List[str]] = None) -> List[Any]:
        """Like find_successor_with_hops, but also records which nodes the lookup passed through."""
        path = (path or []) + [self.address]
        id_val = int(id_str)
        if in_half_open_range(id_val, self.node_id, self.successor_id):
            return [self.successor, hop_count, path]
        else:
            n0_addr = self.closest_preceding_node(id_val)
            if n0_addr == self.address:
                return [self.successor, hop_count, path]
            try:
                return await self._rpc(n0_addr, "find_successor_traced", str(id_val), hop_count + 1, path)
            except Exception:
                return [self.successor, hop_count, path]

    def get_finger_table(self) -> List[Dict[str, Any]]:
        """Finger table compressed into ranges: consecutive fingers pointing at the same node are grouped."""
        groups: List[Dict[str, Any]] = []
        for i, addr in enumerate(self.finger_table):
            if groups and groups[-1]["node"] == addr:
                groups[-1]["to_finger"] = i
            else:
                groups.append({
                    "from_finger": i,
                    "to_finger": i,
                    "start_hash": str((self.node_id + (2 ** i)) % (2 ** self.m)),
                    "node": addr,
                })
        return groups

    def closest_preceding_node(self, id_val: int) -> str:
        for i in range(self.m - 1, -1, -1):
            finger_addr = self.finger_table[i]
            if finger_addr:
                finger_id = self.get_addr_hash(finger_addr)
                if in_open_range(finger_id, self.node_id, id_val):
                    return finger_addr
        return self.address

    async def notify(self, potential_predecessor: str):
        p_id = self.get_addr_hash(potential_predecessor)
        predecessor_changed = False
        if self.predecessor is None or self.predecessor == self.address:
            self.predecessor = potential_predecessor
            predecessor_changed = True
        else:
            pred_id = self.get_addr_hash(self.predecessor)
            if in_open_range(p_id, pred_id, self.node_id):
                self.predecessor = potential_predecessor
                predecessor_changed = True

        if predecessor_changed:
            await self.sync_replicas_to_successors(force=True)

    def store_replica(self, cluster_id: int, value: str) -> bool:
        cid_str = str(cluster_id)
        if cid_str not in self.storage:
            self.storage[cid_str] = {}

        try:
            new_id = json.loads(value).get("course_id")
            self.storage[cid_str][new_id] = value
            self._replica_sync_dirty = True
        except Exception:
            pass

        return True

    def store_bulk(self, items: List[Any]) -> int:
        """Batched direct storage for offline bulk loading (index construction).
        items: list of [cluster_id, course_json]. Stores primaries only (no
        replica forwarding) -- the loader targets each owner directly."""
        n = 0
        for cid, cjson in items:
            if self.store_replica(int(cid), cjson):
                n += 1
        return n

    async def store_local(self, cluster_id: int, value: str) -> bool:
        """Locally stores a course value and forwards a replica to its successor."""
        success = self.store_replica(cluster_id, value)
        if self.rf > 0 and success and self.successor != self.address:
            try:
                await self._rpc(self.successor, "store_replica", cluster_id, value)
            except Exception as e:
                print(f"[{self.address}] Failed to replicate cluster {cluster_id} to {self.successor}: {e}")
        return success

    async def retrieve_local(self, cluster_id: int, is_replica_request: bool = False) -> List[str]:
        if str(cluster_id) in self.storage:
            return list(self.storage[str(cluster_id)].values())
        return []

    async def retrieve_local_adjacent(self, cluster_ids: List[int]) -> Dict[str, Any]:
        """
        Returns results for the clusters this node owns, plus a per-neighbour
        forwarding map for the rest -- each un-owned cluster is forwarded ONE
        ring step, toward the successor or the predecessor (whichever is the
        shorter way round to the cluster's hash), NOT via find_successor.

        This is the cheap contiguous-arc walk that keeps semantic's hop count
        low. A query's nprobe clusters form a short contiguous arc on the ring
        (get_cluster_hash maps cluster IDs linearly, and
        _vectorize_and_find_centroids returns best +/- radius), so the arc is
        collected from a handful of consecutive owners by walking, instead of
        paying one O(log N) finger lookup per cluster (which is what
        async_clustered does, and why its hop count is far higher). A cluster
        whose forwarding target would degenerate to this node itself is served
        locally rather than silently dropped; genuine gaps (a dead node on the
        arc) are handled by the caller's find_successor fallback on RPC failure.
        """
        owned_results = []
        next_queries: Dict[str, List[int]] = {}

        pred = self.predecessor if (self.predecessor and self.predecessor != self.address) else None
        pred_id = self.get_addr_hash(pred) if pred else None
        succ = self.successor if self.successor != self.address else None
        succ_id = self.get_addr_hash(succ) if succ else None
        half = 2 ** (self.m - 1)

        for cid in cluster_ids:
            h = self.get_cluster_hash(cid)

            if pred_id is not None:
                owns_it = in_half_open_range(h, pred_id, self.node_id)
            else:
                owns_it = str(cid) in self.storage

            if owns_it:
                owned_results.extend(await self.retrieve_local(cid))
                continue

            if succ_id is not None and in_half_open_range(h, self.node_id, succ_id):
                target = succ
            else:
                cw = (h - self.node_id) % (2 ** self.m)
                target = succ if cw <= half else pred

            if target and target != self.address:
                next_queries.setdefault(target, []).append(cid)
            else:
                owned_results.extend(await self.retrieve_local(cid))

        return {
            "results": owned_results,
            "next_queries": next_queries
        }

    def claim_and_migrate_data(self, new_node_id_str: str, new_node_address: str) -> Dict[str, List[str]]:
        """
        Called by a joining node on its successor.
        Identifies and returns all cluster data that the new node is responsible for (both primary and replica),
        and removes replica data that this node should no longer keep.
        """
        new_node_id = int(new_node_id_str)
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_addr_hash(pred_addr)
        self_id = self.node_id

        migrated_data = {}
        keys_to_delete = []

        for cid_str, courses in list(self.storage.items()):
            cluster_id = int(cid_str)
            cluster_hash = self.get_cluster_hash(cluster_id)

            if in_half_open_range(cluster_hash, pred_id, new_node_id):
                migrated_data[cid_str] = courses
                if self.r <= 1:
                    keys_to_delete.append(cid_str)

            elif in_half_open_range(cluster_hash, new_node_id, self_id):
                pass

            else:
                migrated_data[cid_str] = courses
                keys_to_delete.append(cid_str)

        for k in keys_to_delete:
            del self.storage[k]

        print(f"[{self.address}] Migrated clusters to new node {new_node_address}. Deleted local replicas for: {keys_to_delete}")
        return migrated_data

    async def sync_replicas_to_successors(self, force: bool = False):
        """Pushes all primary data from this node to its successor list as replicas.
        Batched into one store_bulk RPC per successor per round instead of one
        store_replica RPC per course -- at real corpus scale (tens of thousands
        of courses per node) the unbatched form re-sent every stabilize_interval_sec
        would mean tens of thousands of RPC round-trips per second per node.

        Still O(primary set size) per call, so the routine periodic tick only
        runs it when primary data actually changed since the last sync
        (self._replica_sync_dirty). Topology-change branches in stabilize()/notify()
        pass force=True since a new successor may not have any data yet regardless
        of whether this node's own primary set changed."""
        if not force and not self._replica_sync_dirty:
            return
        if self.rf <= 0 or self.successor == self.address:
            return

        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_addr_hash(pred_addr)

        items = []
        for cid_str, courses in list(self.storage.items()):
            cluster_id = int(cid_str)
            cat_hash = self.get_cluster_hash(cluster_id)
            if in_half_open_range(cat_hash, pred_id, self.node_id):
                for course_str in list(courses.values()):
                    items.append([cluster_id, course_str])
        if not items:
            self._replica_sync_dirty = False
            return

        had_failure = False
        for succ in self.successors[:self.rf]:
            if succ == self.address:
                continue
            try:
                await self._rpc(succ, "store_bulk", items)
            except Exception as e:
                print(f"[{self.address}] Failed to sync replicas to {succ}: {e}")
                had_failure = True
        self._replica_sync_dirty = had_failure

    def get_info(self) -> Dict[str, Any]:
        """Returns metadata about the node's status on the ring."""
        primary_summary = {}
        replica_summary = {}

        pred_id = self.get_addr_hash(self.predecessor) if self.predecessor else self.node_id

        for cid_str, courses in self.storage.items():
            cluster_id = int(cid_str)
            cat_hash = self.get_cluster_hash(cluster_id)
            if in_half_open_range(cat_hash, pred_id, self.node_id):
                primary_summary[cid_str] = len(courses)
            else:
                replica_summary[cid_str] = len(courses)

        return {
            "address": self.address,
            "node_id": str(self.node_id),
            "successor": self.successor,
            "predecessor": self.predecessor,
            "primary_summary": primary_summary,
            "replica_summary": replica_summary
        }

    # --- Client / Node API ---

    async def join(self, bootstrap_addr: Optional[str]) -> bool:
        if bootstrap_addr:
            try:
                try:
                    # Sync the DATA replication factor from the bootstrap so the whole
                    # ring agrees how many copies of a document exist. Do NOT touch
                    # self.r here: that is the successor-LIST length (ring fault
                    # tolerance), configured locally via dht.successor_list_size.
                    # Clobbering it reset every joining node's list back to rf --
                    # precisely the length the RF+1-adjacent-kill failure model is
                    # guaranteed to exhaust, which sends stabilize() into the
                    # 160-finger rescue scan and stalls healing indefinitely.
                    self.rf = await self._rpc(bootstrap_addr, "get_replication_factor")
                except Exception:
                    pass
                succ = await self._rpc(bootstrap_addr, "find_successor", str(self.node_id))

                self.successors = [succ]
                self.finger_table[0] = succ

                try:
                    s_list = await self._rpc(succ, "get_successor_list")
                    new_list = [succ]
                    for n in s_list:
                        if n not in new_list and n != self.address:
                            new_list.append(n)
                    self.successors = new_list[:self.r]
                except Exception:
                    pass

                if self.successor != self.address:
                    print(f"[{self.address}] Requesting data migration from successor {self.successor}...")
                    try:
                        migrated_data = await self._rpc(self.successor, "claim_and_migrate_data", str(self.node_id), self.address)
                        for cid_str, courses in migrated_data.items():
                            self.storage[cid_str] = courses
                        print(f"[{self.address}] Data migration complete. Received {len(migrated_data)} clusters.")
                    except Exception as e:
                        print(f"[{self.address}] Warning: Data migration failed: {e}")

                return True
            except Exception as e:
                print(f"[{self.address}] Failed to join ring via {bootstrap_addr}: {e}")
                return False
        else:
            self.successors = [self.address]
            self.finger_table[0] = self.address
            self.predecessor = None
            return True

    async def put_course(self, course_json: str) -> bool:
        """Calculates cluster and routes to the responsible node in the DHT."""
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"

        course["vector"] = self._vectorize(text)
        course_json_with_vec = json.dumps(course)

        top_clusters = self._vectorize_and_find_centroids(text, nprobe=1)
        cluster_id = top_clusters[0]

        cluster_hash = self.get_cluster_hash(cluster_id)
        target_node = await self.find_successor(str(cluster_hash))

        if target_node == self.address:
            return await self.store_local(cluster_id, course_json_with_vec)
        else:
            try:
                return await self._rpc(target_node, "store_local", cluster_id, course_json_with_vec)
            except Exception as e:
                print(f"[{self.address}] Failed to route PUT for cluster {cluster_id} to {target_node}: {e}")
                return False

    async def get_similar_courses(self, course_json: str, nprobe: int = 1, return_hops: bool = False, return_trace: bool = False) -> Any:
        """Finds top `nprobe` clusters, routes GETs, and returns Top-5 courses using Cosine Similarity."""
        if return_trace:
            return_hops = True
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"

        print(f"[{self.address}] Received similarity query. Running internal vectorization...")
        query_vec = self._vectorize(text)
        top_clusters = self._vectorize_and_find_centroids(text, nprobe=nprobe)
        print(f"[{self.address}] Target semantic clusters found: {top_clusters} (nprobe={nprobe})")

        results = []
        total_hops = 0

        best_cluster = top_clusters[0]
        best_hash = self.get_cluster_hash(best_cluster)

        lookup_path = [self.address]
        if return_trace:
            current_target, hops, lookup_path = await self.find_successor_traced(str(best_hash))
            total_hops += hops
        elif return_hops:
            current_target, hops = await self.find_successor_with_hops(str(best_hash))
            total_hops += hops
        else:
            current_target = await self.find_successor(str(best_hash))

        pending_queries = {current_target: list(top_clusters)}
        served = set()
        hop_counted_nodes = {current_target}
        # Per-cluster set of nodes already asked, so the arc-walk can never
        # forward a cluster back to a node that already handled it (breaks any
        # A->B->A oscillation and guarantees the walk terminates).
        asked = {c: {current_target} for c in top_clusters}

        trace = None
        if return_trace:
            trace = {
                "entry_node": self.address,
                "entry_node_id": str(self.node_id),
                "ring_bits": self.m,
                "top_clusters": [int(c) for c in top_clusters],
                "cluster_hashes": {str(cid): str(self.get_cluster_hash(cid)) for cid in top_clusters},
                "primary_cluster": int(best_cluster),
                "primary_owner": current_target,
                "chord_hops_to_owner": total_hops,
                "lookup_path": lookup_path,
                "nodes_contacted": [],
            }

        while pending_queries:
            target_node = list(pending_queries.keys())[0]
            requested = pending_queries.pop(target_node)

            requested = [c for c in requested if c not in served]
            if not requested:
                continue

            if trace is not None:
                trace["nodes_contacted"].append({"node": target_node, "clusters": [int(c) for c in requested]})

            if target_node not in hop_counted_nodes:
                if return_hops and target_node != self.address:
                    total_hops += 1
                hop_counted_nodes.add(target_node)

            if target_node == self.address:
                resp = await self.retrieve_local_adjacent(requested)
            else:
                print(f" └──> [{self.address}] Adjacent routing request for {requested} directly to {target_node}...")
                try:
                    resp = await self._rpc(target_node, "retrieve_local_adjacent", requested)
                except Exception as e:
                    print(f"[{self.address}] Failed adjacent fetch from {target_node}: {e}")
                    for cid in requested:
                        fallback_node = await self.find_successor(str(self.get_cluster_hash(cid)))
                        pending_queries.setdefault(fallback_node, []).append(cid)
                    continue

            results.extend(resp["results"])
            forwarded = set()
            for next_node, cids in resp["next_queries"].items():
                fresh = []
                for c in cids:
                    if next_node in asked.get(c, ()):
                        continue  # already asked this node for this cluster; stop looping
                    asked.setdefault(c, set()).add(next_node)
                    fresh.append(c)
                    forwarded.add(c)
                if fresh:
                    pending_queries.setdefault(next_node, []).extend(fresh)
            served.update(c for c in requested if c not in forwarded)

        unique_results = list(set(results))

        # Rank by cosine similarity. The per-candidate 1000-dim dot product is
        # batched into a single numpy matmul: at high nprobe the candidate set
        # is large (the doomed cluster alone is ~1600 courses), and a
        # pure-Python dot per course blows the query timeout. Stored vectors
        # are reused when present (embed_vectors); otherwise the row is built
        # once via _vectorize.
        parsed = [json.loads(cs) for cs in unique_results]
        if parsed:
            qv = np.asarray(query_vec, dtype=np.float32)
            mat = np.empty((len(parsed), self.n_features), dtype=np.float32)
            for r, c in enumerate(parsed):
                v = c.pop("vector", None)
                if v is not None:
                    mat[r] = v
                else:
                    mat[r] = self._vectorize(f"{c['course_title']} {c['category']} {c['description']}")
            sims = mat @ qv
            order = sorted(range(len(parsed)), key=lambda i: (-float(sims[i]), str(parsed[i].get("course_id", ""))))
            final_list = []
            for i in order[:5]:
                parsed[i]["similarity"] = float(sims[i])
                final_list.append(json.dumps(parsed[i]))
        else:
            final_list = []
        if return_trace:
            return final_list, total_hops, trace
        if return_hops:
            return final_list, total_hops
        return final_list

    # --- Background Stabilization Protocols ---

    async def stabilize(self):
        if self.successor == self.address:
            if self.predecessor and self.predecessor != self.address:
                self.successors = [self.predecessor]
                self.finger_table[0] = self.successor
                await self.sync_replicas_to_successors(force=True)
            return

        alive_successor = None
        for succ in list(self.successors):
            try:
                await self._rpc(succ, "ping")
                self._succ_miss[succ] = 0
                alive_successor = succ
                break
            except Exception:
                misses = self._succ_miss.get(succ, 0) + 1
                self._succ_miss[succ] = misses
                if misses >= self._succ_miss_threshold:
                    print(f"[{self.address}] Successor {succ} failed {misses}x consecutively. Removing from list.")
                    if succ in self.successors:
                        self.successors.remove(succ)
                    self._succ_miss.pop(succ, None)
                # else: transient miss below threshold -- keep it, try the next.

        if not alive_successor:
            # No successor answered this tick. Only fall back to the disruptive
            # finger-based recovery if the successor list is now actually empty
            # (every successor exceeded the miss threshold = confirmed dead). If
            # entries remain, the failures were transient -- keep the current
            # topology and retry next tick instead of churning it.
            if self.successors:
                return
            found_alive = False
            for i in range(1, self.m):
                finger = self.finger_table[i]
                if finger and finger != self.address:
                    try:
                        await self._rpc(finger, "ping")
                        self.successors = [finger]
                        self.finger_table[0] = finger
                        found_alive = True
                        print(f"[{self.address}] All successors failed. Found alive finger {finger}.")
                        break
                    except Exception:
                        pass
            if not found_alive:
                self.successors = [self.address]
                self.finger_table[0] = self.address

            await self.sync_replicas_to_successors(force=True)
            return

        try:
            x = await self._rpc(alive_successor, "get_predecessor")
            if x:
                x_id = self.get_addr_hash(x)
                if in_open_range(x_id, self.node_id, self.get_addr_hash(alive_successor)):
                    if x not in self.successors:
                        self.successors.insert(0, x)
                    self.successors = self.successors[:self.r]
                    alive_successor = x

            self.finger_table[0] = alive_successor

            try:
                s_list = await self._rpc(alive_successor, "get_successor_list")
                new_list = [alive_successor]
                for n in s_list:
                    if n not in new_list and n != self.address:
                        new_list.append(n)
                self.successors = new_list[:self.r]
            except Exception:
                pass

            await self._rpc(alive_successor, "notify", self.address)

        except Exception:
            pass
        await self.cleanup_stale_replicas()
        await self.sync_replicas_to_successors()

    async def cleanup_stale_replicas(self):
        depth = self.rf + 1
        curr = self.predecessor
        valid = True
        for _ in range(depth - 1):
            if curr and curr != self.address:
                try:
                    curr = await self._rpc(curr, "get_predecessor")
                except Exception:
                    valid = False
                    break
            else:
                valid = False
                break

        if valid and curr:
            limit_id = self.get_addr_hash(curr)
            keys_to_delete = []
            for k_str in list(self.storage.keys()):
                key_id = self.get_cluster_hash(int(k_str))
                if not in_half_open_range(key_id, limit_id, self.node_id) and self.predecessor is not None:
                    keys_to_delete.append(k_str)
            for k in keys_to_delete:
                del self.storage[k]

    FINGERS_PER_ROUND = 8

    def _redundant_finger_cutoff(self) -> int:
        """Index of the first finger whose target can lie beyond our own successor.

        Finger i targets node_id + 2^i. While that offset still falls inside the
        arc (node_id, successor], find_successor() answers `self.successor` from
        its first branch -- locally, without an RPC -- so the entry holds nothing
        the successor pointer does not already say. On a ring of N nodes the arc
        averages 2^m / N, leaving only the top O(log N) fingers able to differ;
        everything below is a fixed prefix that moves only when the successor does.

        Finger VALUES are unaffected by using this boundary: the prefix is still
        written, taken from the successor pointer instead of from a lookup that
        would have returned it anyway. What shrinks is the SWEEP -- m entries
        (20 rounds at 8/round, so ~20s before is_finger_stable() can first report
        True) become O(log N) entries, a couple of rounds. Since
        wait_for_finger_stability() requires every node to report a completed
        zero-change sweep at the same time, sweep length is what sets convergence
        time, and how attainable that global condition stays as N grows."""
        gap = (self.successor_id - self.node_id) % (2 ** self.m)
        if gap == 0:
            # Sole node on the ring (successor is self): no finger can point
            # anywhere else, so the prefix is the entire table.
            return self.m
        return min(self.m, gap.bit_length())

    async def fix_fingers(self):
        """Sequential finger repair (the Chord paper's next-counter variant, not
        random sampling), several entries per stabilization round -- restricted to
        the entries that can actually differ (see _redundant_finger_cutoff)."""
        if not hasattr(self, "_fix_next"):
            self._fix_next = 0
            self._sweep_changes = 0
            self._last_sweep_changes = -1

        cutoff = self._redundant_finger_cutoff()

        # Re-point the redundant prefix at the current successor every round. It
        # costs no RPC, and it has to be eager rather than one entry per round: a
        # successor change invalidates the whole prefix at once. Counting those
        # writes also stops a node from claiming a fixpoint while its successor
        # is still moving.
        for i in range(cutoff):
            if self.finger_table[i] != self.successor:
                self.finger_table[i] = self.successor
                self._sweep_changes += 1

        if cutoff >= self.m:
            # Nothing left to resolve -- the prefix above is the whole table.
            self._last_sweep_changes = self._sweep_changes
            self._sweep_changes = 0
            return

        for _ in range(self.FINGERS_PER_ROUND):
            # The cutoff moves with the successor, so re-anchor if it drifted out
            # of the resolvable range.
            if not (cutoff <= self._fix_next < self.m):
                self._fix_next = cutoff
            i = self._fix_next
            target_id = (self.node_id + (2 ** i)) % (2 ** self.m)
            try:
                new_finger = await self.find_successor(str(target_id))
                if self.finger_table[i] != new_finger:
                    self.finger_table[i] = new_finger
                    self._sweep_changes += 1
            except Exception:
                self._sweep_changes += 1
            self._fix_next += 1
            if self._fix_next >= self.m:
                self._fix_next = cutoff
                self._last_sweep_changes = self._sweep_changes
                self._sweep_changes = 0

    def is_finger_stable(self) -> bool:
        """True once the most recent complete fix_fingers sweep produced zero
        changes -- this node's routing state has reached a fixpoint."""
        return getattr(self, "_last_sweep_changes", -1) == 0

    async def check_predecessor(self):
        if self.predecessor and self.predecessor != self.address:
            try:
                await self._rpc(self.predecessor, "ping")
            except Exception:
                self.predecessor = None

    # --- Lifecycle Control ---

    async def periodic_loop(self):
        while not self.shutdown_event.is_set():
            try:
                await self.stabilize()
                await self.fix_fingers()
                await self.check_predecessor()
            except Exception:
                pass
            try:
                await asyncio.wait_for(self.shutdown_event.wait(), timeout=self.stabilize_interval)
            except asyncio.TimeoutError:
                pass

    async def start(self):
        self._periodic_task = asyncio.create_task(self.periodic_loop())
        print(f"Node started on {self.address} (ID: {self.node_id})")

    async def stop(self) -> bool:
        self.shutdown_event.set()
        if self._periodic_task:
            try:
                await self._periodic_task
            except Exception:
                pass
        if self._uvicorn_server is not None:
            self._uvicorn_server.should_exit = True
        print(f"Node stopped on {self.address}")
        return True
