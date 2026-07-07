import json
import hashlib
import socket
import threading
import time
import random
import math
import os
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
            
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.k = data["k"]
                self.n_features = data["n_features"]
                self.vocabulary = data["vocabulary"]
                self.centroids = data["centroids"]

    def get_cluster_hash(self, cluster_id: int) -> int:
        """Hashes a cluster ID using SHA-1 to place it randomly on the Chord ring (destroys semantic ring locality)."""
        return self.get_hash(f"cluster_{cluster_id}")

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
            
        distances = []
        for c_id, centroid in enumerate(self.centroids):
            dist = math.sqrt(sum((v - c)**2 for v, c in zip(vec, centroid)))
            distances.append((dist, c_id))
            
        distances.sort(key=lambda x: x[0])
        return [c_id for dist, c_id in distances[:nprobe]]

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

    def _get_rpc_client(self, addr: str) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"http://{addr}", allow_none=True)

    # --- XML-RPC Exposed Methods ---

    def ping(self) -> bool:
        return True

    def get_replication_factor(self) -> int:
        return self.rf

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
            self.sync_replicas_to_successors()

    def store_replica(self, cluster_id: int, value: str) -> bool:
        cid_str = str(cluster_id)
        if cid_str not in self.storage:
            self.storage[cid_str] = {}
            
        try:
            # We must load json ONCE to get the course_id for the O(1) dictionary key
            new_id = json.loads(value).get("course_id")
            self.storage[cid_str][new_id] = value
        except Exception:
            pass
            
        return True

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

    def sync_replicas_to_successors(self):
        """Pushes all primary data from this node to its successor list as replicas."""
        if self.rf <= 0 or self.successor == self.address:
            return
            
        # Identify what we are Primary for (using predecessor)
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_hash(pred_addr)
        
        for succ in self.successors[:self.rf]:
            if succ == self.address:
                continue
            try:
                with self._get_rpc_client(succ) as succ_client:
                    for cid_str, courses in list(self.storage.items()):
                        cluster_id = int(cid_str)
                        cat_hash = self.get_cluster_hash(cluster_id)
                        
                        # If we are the primary holder of this cluster:
                        if in_half_open_range(cat_hash, pred_id, self.node_id):
                            for course_str in list(courses.values()):
                                succ_client.store_replica(cluster_id, course_str)
            except Exception as e:
                print(f"[{self.address}] Failed to sync replicas to {succ}: {e}")



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

    # --- Client / Node API ---

    def join(self, bootstrap_addr: Optional[str]) -> bool:
        if bootstrap_addr:
            try:
                with self._get_rpc_client(bootstrap_addr) as bootstrap:
                    try:
                        self.r = bootstrap.get_replication_factor()
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

    def get_similar_courses(self, course_json: str, nprobe: int = 1, return_hops: bool = False) -> Any:
        """Finds top `nprobe` clusters, routes GETs, and returns Top-5 courses using Cosine Similarity."""
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"
        
        print(f"[{self.address}] Received similarity query. Running internal vectorization...")
        query_vec = self._vectorize(text)
        top_clusters = self._vectorize_and_find_centroids(text, nprobe=nprobe)
        print(f"[{self.address}] Target semantic clusters found: {top_clusters} (nprobe={nprobe})")
        
        results = []
        total_hops = 0
        unique_nodes = {}  # target_node -> list of cluster_ids
        
        # 1. Resolve successors and deduplicate targets
        for cluster_id in top_clusters:
            cluster_hash = self.get_cluster_hash(cluster_id)
            if return_hops:
                target_node, hops = self.find_successor_with_hops(str(cluster_hash))
                if target_node not in unique_nodes:
                    unique_nodes[target_node] = []
                    total_hops += hops  # Only count routing hops to reach this unique node
            else:
                target_node = self.find_successor(str(cluster_hash))
                if target_node not in unique_nodes:
                    unique_nodes[target_node] = []
            
            unique_nodes[target_node].append(cluster_id)
            
        # 2. Query each unique target node once
        for target_node, cluster_ids in unique_nodes.items():
            if target_node == self.address:
                for cid in cluster_ids:
                    print(f" └──> [{self.address}] Serving cluster {cid} from local disk.")
                    results.extend(self.retrieve_local(cid))
            else:
                print(f" └──> [{self.address}] Routing network request for clusters {cluster_ids} to {target_node}...")
                try:
                    with self._get_rpc_client(target_node) as client:
                        for cid in cluster_ids:
                            results.extend(client.retrieve_local(cid))
                except Exception as e:
                    print(f"[{self.address}] Failed to fetch clusters {cluster_ids} from {target_node}: {e}")
        
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
            
            ranked_results.append((sim, json.dumps(c)))
            
        # Sort descending by similarity
        ranked_results.sort(key=lambda x: x[0], reverse=True)
        
        # Return top 5
        final_list = [c_str for sim, c_str in ranked_results[:5]]
        if return_hops:
            return final_list, total_hops
        return final_list

    # --- Background Stabilization Protocols ---

    def stabilize(self):
        if self.successor == self.address:
            if self.predecessor and self.predecessor != self.address:
                self.successors = [self.predecessor]
                self.finger_table[0] = self.successor
                self.sync_replicas_to_successors()
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
                
            self.sync_replicas_to_successors()
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

    def fix_fingers(self):
        i = random.randint(0, self.m - 1)
        target_id = (self.node_id + (2 ** i)) % (2 ** self.m)
        try:
            self.finger_table[i] = self.find_successor(str(target_id))
        except Exception:
            pass

    def check_predecessor(self):
        if self.predecessor and self.predecessor != self.address:
            try:
                with self._get_rpc_client(self.predecessor) as pred:
                    pred.ping()
            except Exception:
                self.predecessor = None

    # --- Lifecycle Control ---

    def start(self):
        self.server = ThreadedXMLRPCServer((self.ip, self.port), logRequests=False, allow_none=True)
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
                except Exception:
                    pass
                time.sleep(self.stabilize_interval)
                
        self.worker_thread = threading.Thread(target=periodic_worker, daemon=True)
        self.worker_thread.start()
        print(f"Node started on {self.address} (ID: {self.node_id})")

    def stop(self):
        self.shutdown_event.set()
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        print(f"Node stopped on {self.address}")
