import os

# Create directories
os.makedirs("src/naive/simulations", exist_ok=True)

# 1. src/naive/node.py
node_code = """import os
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
from src.config_loader import load_config

class ThreadedXMLRPCServer(ThreadingMixIn, SimpleXMLRPCServer):
    pass

def in_half_open_range(key: int, a: int, b: int) -> bool:
    if a < b: return a < key <= b
    return a < key or key <= b

def in_open_range(key: int, a: int, b: int) -> bool:
    if a < b: return a < key < b
    return a < key or key < b

class NaiveChordNode:
    def __init__(self, ip: str, port: int, bootstrap_node: str = None):
        self.ip = ip
        self.port = port
        self.address = f"{ip}:{port}"
        self.m = 7
        self.node_id = self.get_hash(self.address)
        
        self.predecessor = None
        config = load_config()
        self.r = config['network']['replication_factor']
        
        self.successors = [self.address]
        self.finger_table = [None] * self.m
        
        self.storage = {} # course_hash_str -> [course_json_with_vec]
        
        self.vocab = self._load_vocab(config['storage']['centroids_path'])
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
            vec = [v/norm for v in vec]
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
    def get_replication_factor(self) -> int: return self.r
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

    def store_replica(self, key_id: int, value: str) -> bool:
        k_str = str(key_id)
        if k_str not in self.storage:
            self.storage[k_str] = []
        try:
            new_id = json.loads(value).get("course_id")
            self.storage[k_str] = [c for c in self.storage[k_str] if json.loads(c).get("course_id") != new_id]
        except: pass
        self.storage[k_str].append(value)
        return True

    def store_local(self, key_id: int, value: str) -> bool:
        success = self.store_replica(key_id, value)
        if success and self.successor != self.address:
            try:
                with self._get_rpc_client(self.successor) as succ:
                    succ.store_replica(key_id, value)
            except: pass
        return success

    def sync_replicas_to_successors(self):
        if self.successor == self.address: return
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_hash(pred_addr)
        for succ in self.successors:
            if succ == self.address: continue
            try:
                with self._get_rpc_client(succ) as succ_client:
                    for k_str, courses in list(self.storage.items()):
                        key_id = int(k_str)
                        if in_half_open_range(key_id, pred_id, self.node_id):
                            for course_str in courses:
                                succ_client.store_replica(key_id, course_str)
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
            elif in_half_open_range(key_hash, new_node_id, self_id):
                pass
            else:
                migrated_data[k_str] = courses
                keys_to_delete.append(k_str)
        for k in keys_to_delete: del self.storage[k]
        return migrated_data

    def get_info(self) -> Dict[str, Any]:
        return {
            "address": self.address,
            "node_id": str(self.node_id),
            "keys_stored": len(self.storage),
            "successor": self.successor
        }

    def join(self, bootstrap_addr: Optional[str]) -> bool:
        if bootstrap_addr:
            try:
                with self._get_rpc_client(bootstrap_addr) as bootstrap:
                    try: self.r = bootstrap.get_replication_factor()
                    except: pass
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
            return self.store_local(course_hash, course_json_with_vec)
        else:
            try:
                with self._get_rpc_client(target_node) as client:
                    return client.store_local(course_hash, course_json_with_vec)
            except: return False

    def local_search(self, query_vec: List[float], top_k: int = 5) -> Tuple[List[Tuple[float, str]], List[str]]:
        # Calculate cosine similarity for all LOCAL courses
        results = []
        for k_str, courses in self.storage.items():
            for c_str in courses:
                c_data = json.loads(c_str)
                sim = self._cosine_similarity(query_vec, c_data["vector"])
                results.append((sim, c_str))
                
        results.sort(key=lambda x: x[0], reverse=True)
        
        # Also return all known peers (fingers + successors) to help the crawler find the whole network
        peers = set(self.successors + [f for f in self.finger_table if f])
        peers.discard(self.address)
        peers.discard(None)
        
        return results[:top_k], list(peers)

    def get_similar_courses(self, course_json: str, nprobe: int = 1) -> List[str]:
        # Naive GET requires FLOODING the network because data is randomly scattered
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"
        query_vec = self._vectorize(text)
        
        # To avoid deadlocks in XMLRPC, the originating node acts as the crawler
        visited = set()
        to_visit = {self.address}
        all_results = []
        
        network_hops = 0
        while to_visit:
            current = to_visit.pop()
            if current in visited: continue
            visited.add(current)
            network_hops += 1
            
            try:
                if current == self.address:
                    local_top, neighbors = self.local_search(query_vec, 5)
                else:
                    with self._get_rpc_client(current) as client:
                        local_top, neighbors = client.local_search(query_vec, 5)
                all_results.extend(local_top)
                for n in neighbors:
                    if n not in visited:
                        to_visit.add(n)
            except: pass
            
        # Deduplicate and Sort global results
        unique_results = {}
        for sim, c_str in all_results:
            c_id = json.loads(c_str)["course_id"]
            if c_id not in unique_results or unique_results[c_id][0] < sim:
                unique_results[c_id] = (sim, c_str)
                
        final_list = list(unique_results.values())
        final_list.sort(key=lambda x: x[0], reverse=True)
        
        print(f"[NAIVE SEARCH] Broadcasted query to {network_hops} nodes!")
        return [c_str for sim, c_str in final_list[:5]]

    def _stabilize_loop(self):
        while self.running:
            try: self.stabilize()
            except: pass
            try: self.fix_fingers()
            except: pass
            try: self.check_predecessor()
            except: pass
            time.sleep(1.0)

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
            except: pass
        if not alive_successors:
            self.successors = [self.address]
        else:
            self.successors = alive_successors
            succ = self.successors[0]
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
                                except: pass
                        s_client.notify(self.address)
                except: pass

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
"""
with open("src/naive/node.py", "w") as f:
    f.write(node_code)

# 2. src/naive/simulations/utils.py
utils_code = """import json
import time
from typing import List
from src.naive.node import NaiveChordNode
from src.config_loader import load_config

def load_courses() -> List[dict]:
    config = load_config()
    with open(config['storage']['data_path'], 'r', encoding='utf-8') as f:
        data = json.load(f)
        return data.get('courses', [])

def setup_network(num_nodes: int, base_port: int) -> List[NaiveChordNode]:
    print(f"Setting up Naive DHT network with {num_nodes} nodes...")
    nodes = []
    
    boot_node = NaiveChordNode("127.0.0.1", base_port)
    nodes.append(boot_node)
    
    for i in range(1, num_nodes):
        port = base_port + i
        node = NaiveChordNode("127.0.0.1", port, bootstrap_node=f"127.0.0.1:{base_port}")
        nodes.append(node)
        time.sleep(0.5)
        
    print("Waiting for ring stabilization...")
    time.sleep(3)
    return nodes

def teardown_network(nodes: List[NaiveChordNode]):
    for n in nodes:
        n.stop()
"""
with open("src/naive/simulations/utils.py", "w") as f:
    f.write(utils_code)

# 3. src/naive/simulations/01_naive_routing.py
sim_code = """import json
import time
import sys
import os

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.naive.simulations.utils import setup_network, teardown_network, load_courses
from src.config_loader import load_config

def run():
    print("=== Simulation 01: Naive Routing (Baseline) ===")
    config = load_config()
    nodes = setup_network(10, 9000)
    
    try:
        courses = load_courses()
        print(f"Injecting {len(courses)} courses into the Naive DHT...")
        bootstrap = nodes[0]
        
        for c in courses:
            bootstrap.put_course(json.dumps(c))
            
        print("Data injected. Courses are randomly scattered (O(1) storage balance, but O(N) search overhead).")
        print("-" * 50)
        
        # Test 1: Similarity Search
        query_course = courses[0] # Let's search for similar courses to the first one
        print(f"Searching for courses similar to: {query_course['course_title']}")
        
        start_time = time.time()
        # Random node performs the search
        results_json = nodes[5].get_similar_courses(json.dumps(query_course))
        end_time = time.time()
        
        print(f"\nTop 5 Results (Latency: {end_time - start_time:.4f} seconds):")
        for i, res_str in enumerate(results_json):
            c = json.loads(res_str)
            print(f" {i+1}. {c['course_title']} (ID: {c['course_id']})")
            
        print("\nNotice the [NAIVE SEARCH] print showing that it had to visit almost ALL nodes to find the answer!")
        
    finally:
        teardown_network(nodes)

if __name__ == "__main__":
    run()
"""
with open("src/naive/simulations/01_naive_routing.py", "w") as f:
    f.write(sim_code)

print("Files created successfully!")
