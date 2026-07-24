import json
import hashlib
import socket
import threading
import time
import random
import math
import os
import numpy as np
from typing import List, Dict, Any, Optional
from xmlrpc.server import SimpleXMLRPCServer
from socketserver import ThreadingMixIn
import xmlrpc.client
import sys

class ThreadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    pass

# Ensure src/ is in the python path to import core
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from core import config_loader

# Removed global socket timeout to prevent GIL thread pool backlog timeouts during rapid injection


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
    """A node in the Chord distributed hash table."""

    def __init__(self, ip: str, port: int, m: int = None, r: int = None, dataset: str = "kaggle"):
        self.config = config_loader.load_config()
        self.dataset = dataset
        
        self.ip = ip
        self.port = port
        self.address = f"{ip}:{port}"
        
        self.m = m if m is not None else self.config['dht']['hash_bits_m']
        self.rf = r if r is not None else self.config['dht']['replication_factor']
        self.r = max(1, self.rf)
        self.stabilize_interval = self.config['dht']['stabilize_interval_sec']
        self.timeout = self.config['network']['timeout_sec']
        self.node_id = self.get_hash(self.address)
        
        self.successors = [self.address]
        self.predecessor = None
        self.finger_table = [self.address] * self.m
        self.storage = {}  # Maps str(cluster_id) -> Dict[course_id, course_json]

        # Optional shard persistence (Kubernetes deployment): when SHARD_DIR is
        # set, this identity snapshots its storage to <SHARD_DIR>/<ip>_<port>.json
        # and WARM-STARTS from it on respawn. Because the identity (and thus the
        # owned arc) is stable, the persisted shard is by construction the data
        # this node should hold; the join migration reconciles any delta written
        # during downtime. Unset (all Compose experiments) => completely inert.
        self._shard_path = None
        self._shard_dirty = False
        # Tracks whether primary storage changed since the last replica sync,
        # so the periodic tick can skip re-marshalling/re-sending the whole
        # primary set when nothing changed (see sync_replicas_to_successors).
        self._replica_sync_dirty = False
        _shard_dir = os.environ.get("SHARD_DIR")
        if _shard_dir:
            os.makedirs(_shard_dir, exist_ok=True)
            self._shard_path = os.path.join(_shard_dir, f"{ip}_{port}.json")
            self._load_shard()
        
        self.shutdown_event = threading.Event()
        self.server = None
        self.server_thread = None
        self.worker_thread = None

        # Load balancing metrics
        self.query_load = 0
        self.LOAD_THRESHOLD = self.config['storage']['load_threshold']

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

        # Process-wide artifact cache: with virtual nodes, MANY ChordNode instances
        # live in one process; each parsing and holding its own copy of a large
        # artifact (k=4096 -> ~70MB JSON, ~hundreds of MB as objects) would multiply
        # memory by the vnode count. All identities share one immutable copy.
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
                # precomputed ||c||^2 per centroid for fast euclidean argmin
                "centroid_norms2": (centroids_np * centroids_np).sum(axis=1),
            }
        art = _ARTIFACT_CACHE[path]
        self.k = art["k"]
        self.n_features = art["n_features"]
        self.vocabulary = art["vocabulary"]
        self.centroids = art["centroids"]
        self._centroid_norms2 = art["centroid_norms2"]

        # K and the ring mapping are inseparable: get_cluster_hash() divides the
        # ring into self.k slots, so a mismatch would place clusters at positions
        # that do not correspond to the centroid vectors being compared against.
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
                
        # L2 Normalize
        norm = math.sqrt(sum(v*v for v in vec))
        if norm > 0:
            vec = [v/norm for v in vec]
        return vec

    def _vectorize_and_find_centroids(self, text: str, nprobe: int = 1) -> List[int]:
        """Converts text to vector, calculates distance, returns top `nprobe` cluster IDs."""
        vec = self._vectorize(text)

        # Vectorized euclidean argmin: dist^2 = ||x||^2 + ||c||^2 - 2 x.c, and
        # ||x||^2 is constant across centroids, so argmin(||c||^2 - 2 x.c) suffices.
        # At fine granularity (k=4096, d=1000) the previous pure-python loop cost
        # ~4M multiplications per query; this is a single BLAS matvec.
        x = np.asarray(vec, dtype=np.float32)
        best_c_id = int((self._centroid_norms2 - 2.0 * (self.centroids @ x)).argmin())
        
        # In Semantic Router, force nprobe to return mathematically adjacent ring clusters
        # instead of semantically similar ones, because adjacent clusters are guaranteed
        # to be on the same peer (zero-hop routing), proving topological locality.
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
        return self.get_hash(self.successor)

    def get_hash(self, addr: str) -> int:
        """Determines the SHA-1 hash of an address."""
        if not addr:
            return 0
        return int(hashlib.sha1(addr.encode('utf-8')).hexdigest(), 16)

    def get_addr_hash(self, addr: str) -> int:
        """Alias for get_hash -- sync nodes hash their hostname directly (no
        transport-prefix stripping needed); kept for interface parity with
        the async_* node classes, which use a real-IP-based get_addr_hash so
        evaluate.py's bulk_load_direct() can call either uniformly."""
        return self.get_hash(addr)

    def _get_rpc_client(self, addr: str) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"http://{addr}", allow_none=True)

    # --- XML-RPC Exposed Methods ---

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

    def find_successor(self, id_str: str) -> str:
        id_val = int(id_str)
        if in_half_open_range(id_val, self.node_id, self.successor_id):
            return self.successor
        else:
            n0_addr = self.closest_preceding_node(id_val)
            if n0_addr == self.address:
                return self.successor
            try:
                with self._get_rpc_client(n0_addr) as n0:
                    return n0.find_successor(str(id_val))
            except Exception:
                return self.successor

    def find_successor_with_hops(self, id_str: str, hop_count: int = 0) -> List[Any]:
        id_val = int(id_str)
        if in_half_open_range(id_val, self.node_id, self.successor_id):
            return [self.successor, hop_count]
        else:
            n0_addr = self.closest_preceding_node(id_val)
            if n0_addr == self.address:
                return [self.successor, hop_count]
            try:
                with self._get_rpc_client(n0_addr) as n0:
                    return n0.find_successor_with_hops(str(id_val), hop_count + 1)
            except Exception:
                return [self.successor, hop_count]

    def find_successor_traced(self, id_str: str, hop_count: int = 0, path: Optional[List[str]] = None) -> List[Any]:
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
                with self._get_rpc_client(n0_addr) as n0:
                    return n0.find_successor_traced(str(id_val), hop_count + 1, path)
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
                finger_id = self.get_hash(finger_addr)
                if in_open_range(finger_id, self.node_id, id_val):
                    return finger_addr
        return self.address

    def notify(self, potential_predecessor: str):
        p_id = self.get_hash(potential_predecessor)
        predecessor_changed = False
        if self.predecessor is None or self.predecessor == self.address:
            self.predecessor = potential_predecessor
            predecessor_changed = True
        else:
            pred_id = self.get_hash(self.predecessor)
            if in_open_range(p_id, pred_id, self.node_id):
                self.predecessor = potential_predecessor
                predecessor_changed = True
                
        if predecessor_changed:
            self.sync_replicas_to_successors(force=True)

    def store_replica(self, cluster_id: int, value: str) -> bool:
        cid_str = str(cluster_id)
        if cid_str not in self.storage:
            self.storage[cid_str] = {}
            
        try:
            # We must load json ONCE to get the course_id for the O(1) dictionary key
            new_id = json.loads(value).get("course_id")
            self.storage[cid_str][new_id] = value
            self._shard_dirty = True  # persistence (no-op unless SHARD_DIR is set)
            self._replica_sync_dirty = True
        except Exception:
            pass

        return True

    def _load_shard(self):
        """Warm start: reload this identity's persisted shard, if one exists."""
        if not (self._shard_path and os.path.exists(self._shard_path)):
            return
        try:
            with open(self._shard_path, "r", encoding="utf-8") as f:
                self.storage = json.load(f)
            n = sum(len(v) for v in self.storage.values())
            print(f"[{self.address}] WARM START: loaded shard from persistent volume "
                  f"({n} courses in {len(self.storage)} clusters).")
        except Exception as e:
            print(f"[{self.address}] Shard load failed ({e}); starting cold.")

    def _save_shard(self):
        """Atomically snapshot storage to the persistent volume (write tmp, rename)."""
        if not (self._shard_path and self._shard_dirty):
            return
        try:
            tmp = self._shard_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.storage, f)
            os.replace(tmp, self._shard_path)
            self._shard_dirty = False
        except Exception as e:
            print(f"[{self.address}] Shard save failed: {e}")

    def store_bulk(self, items: List[Any]) -> int:
        """Batched direct storage for offline bulk loading (index construction).
        items: list of [cluster_id, course_json]. Stores primaries only (no
        replica forwarding) -- the loader targets each owner directly."""
        n = 0
        for cid, cjson in items:
            if self.store_replica(int(cid), cjson):
                n += 1
        return n

    def store_local(self, cluster_id: int, value: str) -> bool:
        """Locally stores a course value and forwards a replica to its successor."""
        success = self.store_replica(cluster_id, value)
        if self.rf > 0 and success and self.successor != self.address:
            try:
                with self._get_rpc_client(self.successor) as succ:
                    succ.store_replica(cluster_id, value)
            except Exception as e:
                print(f"[{self.address}] Failed to replicate cluster {cluster_id} to {self.successor}: {e}")
        return success

    def retrieve_local(self, cluster_id: int, is_replica_request: bool = False) -> List[str]:
        # 1. Load Balancing Delegation (Active Replica)
        if not is_replica_request and self.query_load >= self.LOAD_THRESHOLD and self.successor != self.address:
            print(f"[{self.address}] ⚠️ OVERLOADED! (Load: {self.query_load}). Delegating read query to Replica at {self.successor}...")
            try:
                with self._get_rpc_client(self.successor) as succ:
                    return succ.retrieve_local(cluster_id, True)
            except Exception as e:
                print(f"[{self.address}] ❌ Failed to delegate to replica: {e}")
                # Fallback to serving locally if delegation fails
                pass
                
        # 2. Serve Locally
        self.query_load += 1
        if str(cluster_id) in self.storage:
            return list(self.storage[str(cluster_id)].values())
        return []

    def retrieve_local_adjacent(self, cluster_ids: List[int]) -> Dict[str, Any]:
        """
        Retrieves local results for clusters owned by this node.
        Returns the rest mapped directly to successor or predecessor to bypass finger table lookups.
        """
        owned_results = []
        next_queries = {}
        
        pred_id = self.get_hash(self.predecessor) if self.predecessor else None
        
        for cid in cluster_ids:
            h = self.get_cluster_hash(cid)
            if self.predecessor is not None and self.predecessor != self.address and in_half_open_range(h, pred_id, self.node_id):
                owned_results.extend(self.retrieve_local(cid))
            elif self.predecessor is None and str(cid) in self.storage:
                owned_results.extend(self.retrieve_local(cid))
            else:
                if self.predecessor is None:
                    target = self.successor
                else:
                    succ_id = self.get_hash(self.successor)
                    if in_half_open_range(h, self.node_id, succ_id):
                        target = self.successor
                    else:
                        dist_cw = (h - self.node_id) % (2 ** self.m)
                        dist_ccw = (self.node_id - h) % (2 ** self.m)
                        target = self.successor if dist_cw < dist_ccw else self.predecessor
                
                if target != self.address and target is not None:
                    if target not in next_queries:
                        next_queries[target] = []
                    next_queries[target].append(cid)
                    
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
        pred_id = self.get_hash(pred_addr)
        self_id = self.node_id
        
        migrated_data = {}
        keys_to_delete = []
        
        for cid_str, courses in list(self.storage.items()):
            cluster_id = int(cid_str)
            cluster_hash = self.get_cluster_hash(cluster_id)
            
            # Case 1: The cluster belongs to the joining node's primary range
            if in_half_open_range(cluster_hash, pred_id, new_node_id):
                migrated_data[cid_str] = courses
                if self.r <= 1:
                    keys_to_delete.append(cid_str)
                # Keep it locally as a replica (since we are the successor of the new node)
            
            # Case 2: The cluster belongs to the successor's remaining primary range
            elif in_half_open_range(cluster_hash, new_node_id, self_id):
                # Keep it locally as primary, do not migrate
                pass
                
            # Case 3: The cluster is replica data that the new node should now replicate instead of us
            else:
                migrated_data[cid_str] = courses
                keys_to_delete.append(cid_str)
                
        for k in keys_to_delete:
            del self.storage[k]
            
        print(f"[{self.address}] Migrated clusters to new node {new_node_address}. Deleted local replicas for: {keys_to_delete}")
        return migrated_data

    def sync_replicas_to_successors(self, force: bool = False):
        """Pushes all primary data from this node to its successor list as replicas.
        Batched into one store_bulk RPC per successor per round instead of one
        store_replica RPC per course -- at real corpus scale (tens of thousands
        of courses per node) the unbatched form re-sent every stabilize_interval_sec
        would mean tens of thousands of RPC round-trips per second per node.

        Still O(primary set size) per call, so the routine periodic tick (called
        unconditionally from stabilize()) only runs it when primary data actually
        changed since the last sync (self._replica_sync_dirty). At full-corpus
        scale a node can own tens of thousands of courses with embedded vectors;
        re-marshalling and re-sending that whole set every second forever (as
        this used to do) costs 1GB+ and ~20s of CPU per tick, indefinitely,
        starving real query handling. Topology changes (new/changed successor)
        pass force=True since the new successor may not have any data yet,
        regardless of whether this node's own primary set changed."""
        if not force and not self._replica_sync_dirty:
            return
        if self.rf <= 0 or self.successor == self.address:
            return

        # Identify what we are Primary for (using predecessor)
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_hash(pred_addr)

        items = []
        for cid_str, courses in list(self.storage.items()):
            cluster_id = int(cid_str)
            cat_hash = self.get_cluster_hash(cluster_id)
            # If we are the primary holder of this cluster:
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
                with self._get_rpc_client(succ) as succ_client:
                    succ_client.store_bulk(items)
            except Exception as e:
                print(f"[{self.address}] Failed to sync replicas to {succ}: {e}")
                had_failure = True
        # Leave dirty=True on failure so the next tick retries; clear it on
        # success so the routine periodic tick goes back to skipping.
        self._replica_sync_dirty = had_failure



    def get_info(self) -> Dict[str, Any]:
        """Returns metadata about the node's status on the ring."""
        primary_summary = {}
        replica_summary = {}
        
        pred_id = self.get_hash(self.predecessor) if self.predecessor else self.node_id
        
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

    def set_load_threshold(self, threshold: int) -> bool:
        self.LOAD_THRESHOLD = threshold
        print(f"[{self.address}] Query delegation threshold updated to {threshold}")
        return True

    # --- Client / Node API ---

    def join(self, bootstrap_addr: Optional[str]) -> bool:
        if bootstrap_addr:
            try:
                with self._get_rpc_client(bootstrap_addr) as bootstrap:
                    try:
                        self.r = bootstrap.get_replication_factor()
                        self.rf = self.r
                    except Exception:
                        pass
                    succ = bootstrap.find_successor(str(self.node_id))
                
                self.successors = [succ]
                self.finger_table[0] = succ
                
                try:
                    with self._get_rpc_client(succ) as s_client:
                        s_list = s_client.get_successor_list()
                        new_list = [succ]
                        for n in s_list:
                            if n not in new_list and n != self.address:
                                new_list.append(n)
                        self.successors = new_list[:self.r]
                except Exception:
                    pass
                
                # Request data migration from successor on join
                if self.successor != self.address:
                    print(f"[{self.address}] Requesting data migration from successor {self.successor}...")
                    try:
                        with self._get_rpc_client(self.successor) as succ:
                            migrated_data = succ.claim_and_migrate_data(str(self.node_id), self.address)
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

    def put_course(self, course_json: str) -> bool:
        """Calculates cluster and routes to the responsible node in the DHT."""
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"
        
        # Pre-compute and embed the vector during insertion to save CPU at query time!
        course["vector"] = self._vectorize(text)
        course_json_with_vec = json.dumps(course)
        
        top_clusters = self._vectorize_and_find_centroids(text, nprobe=1)
        cluster_id = top_clusters[0]
        
        cluster_hash = self.get_cluster_hash(cluster_id)
        target_node = self.find_successor(str(cluster_hash))
        
        if target_node == self.address:
            return self.store_local(cluster_id, course_json_with_vec)
        else:
            try:
                with self._get_rpc_client(target_node) as client:
                    return client.store_local(cluster_id, course_json_with_vec)
            except Exception as e:
                print(f"[{self.address}] Failed to route PUT for cluster {cluster_id} to {target_node}: {e}")
                return False

    def get_similar_courses(self, course_json: str, nprobe: int = 1, return_hops: bool = False, return_trace: bool = False) -> Any:
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
        
        # 1. Resolve successor for the first (best) cluster using standard O(log N) finger table
        best_cluster = top_clusters[0]
        best_hash = self.get_cluster_hash(best_cluster)
        
        lookup_path = [self.address]
        if return_trace:
            current_target, hops, lookup_path = self.find_successor_traced(str(best_hash))
            total_hops += hops
        elif return_hops:
            current_target, hops = self.find_successor_with_hops(str(best_hash))
            total_hops += hops
        else:
            current_target = self.find_successor(str(best_hash))
            
        # 2. Iteratively retrieve adjacent clusters using direct 1-hop links
        pending_queries = {current_target: list(top_clusters)}
        served = set()  # clusters already retrieved; replaces node-level 'visited'
        hop_counted_nodes = {current_target}

        # Routing trace for the API gateway: hashes are stringified because
        # they exceed XML-RPC's integer range.
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

            # Only ask for clusters we have NOT already retrieved. This replaces the
            # old node-level 'visited' guard, which dropped every cluster routed to an
            # already-contacted node -> lost results and depressed recall at high nprobe.
            # Tracking served clusters instead never drops one, and still terminates
            # (each forwarded cluster moves monotonically toward its owner, then is served).
            requested = [c for c in requested if c not in served]
            if not requested:
                continue

            if trace is not None:
                trace["nodes_contacted"].append({"node": target_node, "clusters": [int(c) for c in requested]})

            if target_node not in hop_counted_nodes:
                # Serving from the entry node itself is a local read: no network hop.
                if return_hops and target_node != self.address:
                    total_hops += 1  # 1 hop to reach this successor/predecessor
                hop_counted_nodes.add(target_node)

            if target_node == self.address:
                resp = self.retrieve_local_adjacent(requested)
            else:
                print(f" └──> [{self.address}] Adjacent routing request for {requested} directly to {target_node}...")
                try:
                    with self._get_rpc_client(target_node) as client:
                        resp = client.retrieve_local_adjacent(requested)
                except Exception as e:
                    print(f"[{self.address}] Failed adjacent fetch from {target_node}: {e}")
                    # Fallback: route each remaining cluster by a direct Chord lookup.
                    for cid in requested:
                        fallback_node = self.find_successor(str(self.get_cluster_hash(cid)))
                        pending_queries.setdefault(fallback_node, []).append(cid)
                    continue

            results.extend(resp["results"])
            # Clusters this node forwarded onward are not yet served; every other
            # requested cluster WAS served here -> mark it so it is never re-fetched
            # or dropped.
            forwarded = set()
            for next_node, cids in resp["next_queries"].items():
                forwarded.update(cids)
                # Queue EVERY forward target, INCLUDING the entry node itself: the
                # loop serves self.address locally. The old skip here silently
                # discarded any cluster whose chain pointed back at the entry --
                # truncating the whole chain beyond it and depressing recall at
                # wide fanout (the entry sits inside the probed arc).
                pending_queries.setdefault(next_node, []).extend(cids)
            served.update(c for c in requested if c not in forwarded)
        
        # Deduplicate
        unique_results = list(set(results))
        
        # Rank by Cosine Similarity
        ranked_results = []
        for course_str in unique_results:
            c = json.loads(course_str)
            # Use the pre-computed vector! Zero overhead!
            c_vec = c.get("vector", self._vectorize(f"{c['course_title']} {c['category']} {c['description']}"))
            
            # Dot product of two L2 normalized vectors is exactly Cosine Similarity!
            sim = sum(v1 * v2 for v1, v2 in zip(query_vec, c_vec))
            
            # Remove the vector before returning so we don't spam the user's terminal with numbers
            if "vector" in c:
                del c["vector"]
                
            c["similarity"] = sim

            ranked_results.append((sim, str(c.get("course_id", "")), json.dumps(c)))

        # Sort by similarity descending with a DETERMINISTIC tie-break on course_id.
        # Near-duplicate courses produce exact similarity ties at the top-5 boundary;
        # without the tie-break, tie order inherits the unordered set() iteration
        # (per-process hash randomization) and results differ across runs/processes.
        ranked_results.sort(key=lambda x: (-x[0], x[1]))

        # Return top 5
        final_list = [c_str for sim, cid, c_str in ranked_results[:5]]
        if return_trace:
            return final_list, total_hops, trace
        if return_hops:
            return final_list, total_hops
        return final_list

    # --- Background Stabilization Protocols ---

    def stabilize(self):
        if self.successor == self.address:
            if self.predecessor and self.predecessor != self.address:
                self.successors = [self.predecessor]
                self.finger_table[0] = self.successor
                self.sync_replicas_to_successors(force=True)
            return

        alive_successor = None
        for succ in list(self.successors):
            try:
                with self._get_rpc_client(succ) as client:
                    client.ping()
                    alive_successor = succ
                    break
            except Exception:
                print(f"[{self.address}] Successor {succ} failed. Removing from list.")
                if succ in self.successors:
                    self.successors.remove(succ)
                
        if not alive_successor:
            found_alive = False
            for i in range(1, self.m):
                finger = self.finger_table[i]
                if finger and finger != self.address:
                    try:
                        with self._get_rpc_client(finger) as client:
                            client.ping()
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

            self.sync_replicas_to_successors(force=True)
            return
            
        try:
            with self._get_rpc_client(alive_successor) as succ_client:
                x = succ_client.get_predecessor()
                if x:
                    x_id = self.get_hash(x)
                    if in_open_range(x_id, self.node_id, self.get_hash(alive_successor)):
                        if x not in self.successors:
                            self.successors.insert(0, x)
                        self.successors = self.successors[:self.r]
                        alive_successor = x
                
                self.finger_table[0] = alive_successor
                
                try:
                    s_list = succ_client.get_successor_list()
                    new_list = [alive_successor]
                    for n in s_list:
                        if n not in new_list and n != self.address:
                            new_list.append(n)
                    self.successors = new_list[:self.r]
                except Exception:
                    pass
                    
                succ_client.notify(self.address)
                
        except Exception:
            pass
        self.cleanup_stale_replicas()
        self.sync_replicas_to_successors()

    def cleanup_stale_replicas(self):
        depth = self.rf + 1
        curr = self.predecessor
        valid = True
        for _ in range(depth - 1):
            if curr and curr != self.address:
                try:
                    with self._get_rpc_client(curr) as client:
                        curr = client.get_predecessor()
                except:
                    valid = False
                    break
            else:
                valid = False
                break
        
        if valid and curr:
            limit_id = self.get_hash(curr)
            keys_to_delete = []
            for k_str in list(self.storage.keys()):
                key_id = self.get_cluster_hash(int(k_str))
                if not in_half_open_range(key_id, limit_id, self.node_id) and self.predecessor is not None:
                    keys_to_delete.append(k_str)
            for k in keys_to_delete:
                del self.storage[k]

    FINGERS_PER_ROUND = 8

    def fix_fingers(self):
        """Sequential finger repair (the Chord paper's next-counter variant, not
        random sampling), several entries per stabilization round. Tracks whether
        the last COMPLETE sweep over all m fingers changed anything, which gives a
        sound per-node convergence signal: a full sweep touched every finger and
        none moved. Random sampling cannot provide this (a stale finger can stay
        unsampled for arbitrarily many rounds)."""
        if not hasattr(self, "_fix_next"):
            self._fix_next = 0
            self._sweep_changes = 0
            self._last_sweep_changes = -1  # no full sweep completed yet
        for _ in range(self.FINGERS_PER_ROUND):
            i = self._fix_next
            target_id = (self.node_id + (2 ** i)) % (2 ** self.m)
            try:
                new_finger = self.find_successor(str(target_id))
                if self.finger_table[i] != new_finger:
                    self.finger_table[i] = new_finger
                    self._sweep_changes += 1
            except Exception:
                # A failed lookup is not evidence of stability: count it as a change
                # so this sweep cannot be reported clean.
                self._sweep_changes += 1
            self._fix_next += 1
            if self._fix_next >= self.m:
                self._fix_next = 0
                self._last_sweep_changes = self._sweep_changes
                self._sweep_changes = 0

    def is_finger_stable(self) -> bool:
        """True once the most recent complete fix_fingers sweep over all m entries
        produced zero changes -- this node's routing state has reached a fixpoint."""
        return getattr(self, "_last_sweep_changes", -1) == 0

    def check_predecessor(self):
        if self.predecessor and self.predecessor != self.address:
            try:
                with self._get_rpc_client(self.predecessor) as pred:
                    pred.ping()
            except Exception:
                self.predecessor = None

    # --- Lifecycle Control ---

    def start(self):
        # Bind to 0.0.0.0 for external access in containerized environments (unless localhost/127.0.0.1)
        bind_ip = self.ip
        if self.ip not in ["127.0.0.1", "localhost"]:
            bind_ip = "0.0.0.0"
        self.server = ThreadedXMLRPCServer((bind_ip, self.port), logRequests=False, allow_none=True)
        self.server.register_instance(self)
        
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        
        def periodic_worker():
            while not self.shutdown_event.is_set():
                try:
                    self.stabilize()
                    self.fix_fingers()
                    self.check_predecessor()
                    # Cool down CPU load
                    if self.query_load > 0:
                        self.query_load -= 1
                    # Periodic shard snapshot (~every 10 rounds, only if dirty;
                    # no-op unless SHARD_DIR is set).
                    self._worker_ticks = getattr(self, "_worker_ticks", 0) + 1
                    if self._worker_ticks % 10 == 0:
                        self._save_shard()
                except Exception:
                    pass
                time.sleep(self.stabilize_interval)

        self.worker_thread = threading.Thread(target=periodic_worker, daemon=True)
        self.worker_thread.start()
        print(f"Node started on {self.address} (ID: {self.node_id})")

    def stop(self):
        self.shutdown_event.set()
        self._save_shard()  # final snapshot on graceful shutdown (no-op if disabled)
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        print(f"Node stopped on {self.address}")
