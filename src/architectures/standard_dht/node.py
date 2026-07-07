import os
import math
import json
import uuid
import threading
import xmlrpc.client
from xmlrpc.server import SimpleXMLRPCServer
from socketserver import ThreadingMixIn
import hashlib
import time
from typing import List, Dict, Optional, Tuple, Any
from core.config_loader import load_config

class ThreadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    pass

def in_half_open_range(key: int, a: int, b: int) -> bool:
    if a < b: return a < key <= b
    return a < key or key <= b

def in_open_range(key: int, a: int, b: int) -> bool:
    if a < b: return a < key < b
    return a < key or key < b

class NaiveChordNode:
    def __init__(self, ip: str, port: int, bootstrap_node: str = None, dataset: str = "kaggle"):
        self.dataset = dataset
        self.ip = ip
        self.port = port
        self.address = f"{ip}:{port}"
        config = load_config()
        self.m = config['dht']['hash_bits_m']
        self.stabilize_interval = config['dht']['stabilize_interval_sec']
        self.timeout = config['network']['timeout_sec']
        self.node_id = self.get_hash(self.address)
        
        self.predecessor = None
        self.rf = config['dht']['replication_factor']
        self.r = max(1, self.rf)
        
        self.successors = [self.address]
        self.finger_table = [None] * self.m
        
        self.storage = {} # course_hash_str -> [course_json_with_vec]
        
        if self.dataset == "synthetic":
            centroids_path = config['storage']['centroids']['synthetic_path']
        else:
            centroids_path = config['storage']['centroids']['kaggle_dataset_path']
        self.vocab = self._load_vocab(centroids_path)
        self.query_load = 0
        
        self.server = ThreadedXMLRPCServer((ip, port), allow_none=True, logRequests=False)
        self.server.register_instance(self)
        
        self.running = True
        self.t_server = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.t_server.start()
        
        if bootstrap_node:
            self.join(bootstrap_node)
            
        self.t_stab = threading.Thread(target=self._stabilize_loop, daemon=True)
        self.t_stab.start()
        
    def _load_vocab(self, path):
        if not os.path.exists(path): return {}
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f).get('vocabulary', {})
            
    def _vectorize(self, text: str) -> List[float]:
        words = text.lower().split()
        vec = [0.0] * len(self.vocab)
        for w in words:
            if w in self.vocab:
                vec[self.vocab[w]] += 1.0
        norm = math.sqrt(sum(v*v for v in vec))
        if norm > 0:
            return [v/norm for v in vec]
        return vec

    def _cosine_similarity(self, vec1: List[float], vec2: List[float]) -> float:
        return sum(v1 * v2 for v1, v2 in zip(vec1, vec2))

    @property
    def successor(self) -> str:
        return self.successors[0] if self.successors else self.address

    @property
    def successor_id(self) -> int:
        return self.get_hash(self.successor)

    def get_hash(self, addr: str) -> int:
        if not addr: return 0
        return int(hashlib.sha1(addr.encode('utf-8')).hexdigest(), 16) % (2**self.m)

    def _get_rpc_client(self, addr: str) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"http://{addr}", allow_none=True)

    def ping(self) -> bool: return True
    def get_replication_factor(self) -> int: return self.rf
    def get_successor_list(self) -> List[str]: return self.successors
    def get_successor(self) -> str: return self.successor
    def get_predecessor(self) -> Optional[str]: return self.predecessor

    def find_successor(self, id_str: str) -> str:
        id_val = int(id_str)
        if in_half_open_range(id_val, self.node_id, self.successor_id):
            return self.successor
        else:
            n0_addr = self.closest_preceding_node(id_val)
            if n0_addr == self.address: return self.successor
            try:
                with self._get_rpc_client(n0_addr) as n0:
                    return n0.find_successor(str(id_val))
            except:
                return self.successor

    def find_successor_with_hops(self, id_str: str, hop_count: int = 0) -> List[Any]:
        id_val = int(id_str)
        if in_half_open_range(id_val, self.node_id, self.successor_id):
            return [self.successor, hop_count]
        else:
            n0_addr = self.closest_preceding_node(id_val)
            if n0_addr == self.address: return [self.successor, hop_count]
            try:
                with self._get_rpc_client(n0_addr) as n0:
                    return n0.find_successor_with_hops(str(id_val), hop_count + 1)
            except:
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

    def store_replica(self, key_id_str: str, value: str) -> bool:
        k_str = str(key_id_str)
        if k_str not in self.storage:
            self.storage[k_str] = []
        try:
            new_id = json.loads(value).get("course_id")
            self.storage[k_str] = [c for c in self.storage[k_str] if json.loads(c).get("course_id") != new_id]
        except: pass
        self.storage[k_str].append(value)
        return True

    def store_local(self, key_id_str: str, value: str) -> bool:
        k_str = str(key_id_str)
        success = self.store_replica(k_str, value)
        if self.rf > 0 and success and self.successor != self.address:
            try:
                with self._get_rpc_client(self.successor) as succ:
                    succ.store_replica(k_str, value)
            except: pass
        return success

    def sync_replicas_to_successors(self):
        if self.rf <= 0 or self.successor == self.address: return
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_hash(pred_addr)
        for succ in self.successors[:self.rf]:
            if succ == self.address: continue
            try:
                with self._get_rpc_client(succ) as succ_client:
                    for k_str, courses in list(self.storage.items()):
                        key_id = int(k_str)
                        if in_half_open_range(key_id, pred_id, self.node_id):
                            for course_str in courses:
                                succ_client.store_replica(k_str, course_str)
            except: pass

    def claim_and_migrate_data(self, new_node_id_str: str, new_node_address: str) -> Dict[str, List[str]]:
        new_node_id = int(new_node_id_str)
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_hash(pred_addr)
        self_id = self.node_id
        migrated_data = {}
        keys_to_delete = []
        for k_str, courses in list(self.storage.items()):
            key_hash = int(k_str)
            if in_half_open_range(key_hash, pred_id, new_node_id):
                migrated_data[k_str] = courses
                if self.r <= 1:
                    keys_to_delete.append(k_str)
            elif in_half_open_range(key_hash, new_node_id, self_id):
                pass
            else:
                migrated_data[k_str] = courses
                keys_to_delete.append(k_str)
        print(f"[{self.address}] claim_and_migrate_data called by {new_node_address}. self.r = {self.r}, keys_to_delete = {len(keys_to_delete)}/{len(self.storage)}")
        for k in keys_to_delete: del self.storage[k]
        return migrated_data

    def get_info(self) -> Dict[str, Any]:
        primary_count = 0
        replica_count = 0
        pred_id = self.get_hash(self.predecessor) if self.predecessor else self.node_id
        
        for k_str, courses in self.storage.items():
            key_id = int(k_str)
            if in_half_open_range(key_id, pred_id, self.node_id):
                primary_count += len(courses)
            else:
                replica_count += len(courses)

        return {
            "address": self.address,
            "node_id": str(self.node_id),
            "keys_stored": len(self.storage),
            "successor": self.successor,
            "predecessor": self.predecessor,
            "primary_count": primary_count,
            "replica_count": replica_count
        }

    def join(self, bootstrap_addr: Optional[str]) -> bool:
        if bootstrap_addr:
            try:
                with self._get_rpc_client(bootstrap_addr) as bootstrap:
                    try:
                        self.r = bootstrap.get_replication_factor()
                        self.rf = self.r
                    except:
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
                except: pass
                if self.successor != self.address:
                    try:
                        with self._get_rpc_client(self.successor) as succ_c:
                            migrated_data = succ_c.claim_and_migrate_data(str(self.node_id), self.address)
                            for k_str, courses in migrated_data.items():
                                self.storage[k_str] = courses
                    except: pass
                return True
            except: return False
        else:
            self.successors = [self.address]
            self.finger_table[0] = self.address
            self.predecessor = None
            return True

    def put_course(self, course_json: str) -> bool:
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"
        course["vector"] = self._vectorize(text)
        course_json_with_vec = json.dumps(course)
        
        # Naive: hash the course_id instead of semantic clustering
        course_hash = self.get_hash(course['course_id'])
        target_node = self.find_successor(str(course_hash))
        
        if target_node == self.address:
            return self.store_local(str(course_hash), course_json_with_vec)
        else:
            try:
                with self._get_rpc_client(target_node) as client:
                    return client.store_local(str(course_hash), course_json_with_vec)
            except: return False

    def local_search_ring(self, query_vec: List[float], top_k: int = 5) -> Tuple[List[Tuple[float, str]], str]:
        # Calculate cosine similarity ONLY for PRIMARY courses to avoid duplicate processing in the ring
        results = []
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_hash(pred_addr)
        
        for k_str, courses in self.storage.items():
            key_id = int(k_str)
            if in_half_open_range(key_id, pred_id, self.node_id) or self.predecessor is None:
                for c_str in courses:
                    c_data = json.loads(c_str)
                    sim = self._cosine_similarity(query_vec, c_data["vector"])
                    results.append((sim, c_str))
                
        results.sort(key=lambda x: x[0], reverse=True)
        
        # Return local results and the exact NEXT node in the ring (successor)
        return results[:top_k], self.successor

    def get_similar_courses(self, course_json: str, nprobe: int = 1, return_hops: bool = False) -> Any:
        # Naive GET requires traversing the ENTIRE ring sequentially to guarantee 100% recall
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"
        query_vec = self._vectorize(text)
        
        # To avoid deadlocks in XMLRPC, the originating node acts as the crawler around the ring
        visited = set()
        current = self.address
        all_results = []
        network_hops = 0
        
        while current not in visited:
            visited.add(current)
            network_hops += 1
            
            try:
                if current == self.address:
                    local_top, next_node = self.local_search_ring(query_vec, 5)
                    print(f"    -> [Crawler] Node {current} (Successor: {next_node}) searched locally.")
                else:
                    with self._get_rpc_client(current) as client:
                        local_top, next_node = client.local_search_ring(query_vec, 5)
                    print(f"    -> [Crawler] Node {current} (Successor: {next_node}) searched via RPC.")
                        
                titles_with_sim = [f"'{json.loads(c_str)['course_title']}' ({sim:.4f})" for sim, c_str in local_top]
                print(f"       Found local courses: {titles_with_sim}")
                        
                all_results.extend(local_top)
                
                # Break if the ring is broken or points to itself indefinitely
                if not next_node or next_node == current:
                    break
                    
                current = next_node
            except Exception:
                # If a node is unreachable, the ring traversal stops
                break
            
        # Deduplicate and Sort global results
        unique_results = {}
        for sim, c_str in all_results:
            c = json.loads(c_str)
            c_id = c["course_id"]
            if c_id not in unique_results or unique_results[c_id][0] < sim:
                c["similarity"] = sim
                unique_results[c_id] = (sim, json.dumps(c))
                
        final_list = list(unique_results.values())
        final_list.sort(key=lambda x: x[0], reverse=True)
        
        print(f"[NAIVE SEARCH] Traversed ring sequentially! Hops: {network_hops}")
        
        # Remove vector from final results if present to keep it clean
        cleaned_list = []
        for sim, c_str in final_list[:5]:
            c = json.loads(c_str)
            if "vector" in c:
                del c["vector"]
            cleaned_list.append(json.dumps(c))
            
        if return_hops:
            return cleaned_list, network_hops
        return cleaned_list

    def _stabilize_loop(self):
        while self.running:
            try: self.stabilize()
            except: pass
            try: self.fix_fingers()
            except: pass
            try: self.check_predecessor()
            except: pass
            time.sleep(self.stabilize_interval)

    def stabilize(self):
        if not self.successors: return
        alive_successors = []
        for succ in self.successors:
            if succ == self.address:
                alive_successors.append(succ)
                continue
            try:
                with self._get_rpc_client(succ) as s_client:
                    s_client.ping()
                    alive_successors.append(succ)
            except Exception as e:
                print(f"[{self.address}] Failed to ping successor {succ}: {e}")
        if not alive_successors:
            self.successors = [self.address]
        else:
            self.successors = alive_successors
            succ = self.successors[0]
            
            # If successor is self, check predecessor to close the loop
            if succ == self.address:
                if self.predecessor and self.predecessor != self.address:
                    try:
                        with self._get_rpc_client(self.predecessor) as p_client:
                            p_client.ping()
                            self.successors = [self.predecessor]
                            succ = self.predecessor
                    except Exception as e:
                        print(f"[{self.address}] Failed to ping predecessor {self.predecessor}: {e}")
                        self.predecessor = None
            
            if succ != self.address:
                try:
                    with self._get_rpc_client(succ) as s_client:
                        x = s_client.get_predecessor()
                        if x and x != self.address:
                            x_id = self.get_hash(x)
                            if in_open_range(x_id, self.node_id, self.get_hash(succ)):
                                try:
                                    with self._get_rpc_client(x) as x_client:
                                        x_client.ping()
                                        self.successors.insert(0, x)
                                        self.successors = self.successors[:self.r]
                                        succ = x
                                except Exception as e:
                                    print(f"[{self.address}] Failed to ping candidate successor {x}: {e}")
                        s_client.notify(self.address)
                        
                        # Update successor list dynamically
                        try:
                            s_list = s_client.get_successor_list()
                            new_list = [succ]
                            for n in s_list:
                                if n not in new_list and n != self.address:
                                    new_list.append(n)
                            self.successors = new_list[:self.r]
                        except Exception as e:
                            print(f"[{self.address}] Failed to fetch successor list from {succ}: {e}")
                except Exception as e:
                    print(f"[{self.address}] Error during stabilization communication with successor {succ}: {e}")
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
                key_id = int(k_str)
                if not in_half_open_range(key_id, limit_id, self.node_id) and self.predecessor is not None:
                    keys_to_delete.append(k_str)
            for k in keys_to_delete:
                del self.storage[k]

    def fix_fingers(self):
        import random
        i = random.randint(0, self.m - 1)
        target_id = (self.node_id + 2**i) % (2**self.m)
        self.finger_table[i] = self.find_successor(str(target_id))

    def check_predecessor(self):
        if self.predecessor and self.predecessor != self.address:
            try:
                with self._get_rpc_client(self.predecessor) as p_client:
                    p_client.ping()
            except:
                self.predecessor = None

    def stop(self):
        self.running = False
        self.server.shutdown()
        self.server.server_close()
