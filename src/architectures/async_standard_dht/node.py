import os
import math
import json
import socket
import asyncio
import hashlib
import httpx
from typing import List, Dict, Optional, Tuple, Any
from core.config_loader import load_config

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


def in_half_open_range(key: int, a: int, b: int) -> bool:
    if a < b: return a < key <= b
    return a < key or key <= b

def in_open_range(key: int, a: int, b: int) -> bool:
    if a < b: return a < key < b
    return a < key or key < b

class NaiveChordNode:
    """Async FastAPI/httpx transport for the Standard DHT (random SHA-1 per-record
    placement, full-ring sequential crawl on read). Same routing/replication logic
    as architectures.standard_dht.node.NaiveChordNode; outbound peer calls share a
    single persistent, connection-pooled httpx.AsyncClient instead of a fresh
    xmlrpc.client.ServerProxy per call, and lifecycle (join/serve/stabilize) is
    driven by the ASGI app + asyncio event loop instead of raw threads."""

    def __init__(self, ip: str, port: int, dataset: str = "kaggle"):
        self.dataset = dataset
        self.ip = ip
        self.port = port
        self.address = f"{ip}:{port}"
        config = load_config()
        self.m = config['dht']['hash_bits_m']
        self.stabilize_interval = config['dht']['stabilize_interval_sec']
        self.timeout = config['network']['timeout_sec']
        self.node_id = self.get_addr_hash(self.address)

        self.predecessor = None
        self.rf = config['dht']['replication_factor']
        self.r = max(1, self.rf)

        self.successors = [self.address]
        self.finger_table = [None] * self.m

        self.storage = {}  # course_hash_str -> [course_json_with_vec]
        # Tracks whether primary storage changed since the last replica sync,
        # so the periodic tick can skip re-marshalling/re-sending the whole
        # primary set when nothing changed (see sync_replicas_to_successors).
        self._replica_sync_dirty = False

        if self.dataset == "synthetic":
            centroids_path = config['storage']['centroids']['synthetic_path']
        else:
            centroids_path = config['storage']['centroids']['kaggle_dataset_path']
        self.vocab = self._load_vocab(centroids_path)

        self.shutdown_event = asyncio.Event()
        self._periodic_task = None
        self._uvicorn_server = None  # set externally by server.py

        self._http = httpx.AsyncClient(timeout=self.timeout)

    def _load_vocab(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(
                f"Centroid artifact missing: {path}. "
                f"Run 'python3 src/ml/train_centroids.py' first."
            )
        with open(path, 'r', encoding='utf-8') as f:
            vocab = json.load(f).get('vocabulary', {})
        if not vocab:
            raise ValueError(f"Corrupt centroid artifact {path}: empty vocabulary.")
        return vocab

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
        return self.get_addr_hash(self.successor)

    def get_hash(self, val: str) -> int:
        """Determines the plain SHA-1 hash of an arbitrary string (data keys,
        e.g. course_id -- not peer addresses; use get_addr_hash for those)."""
        if not val: return 0
        return int(hashlib.sha1(val.encode('utf-8')).hexdigest(), 16) % (2**self.m)

    def get_addr_hash(self, addr: str) -> int:
        """Determines the SHA-1 hash of a peer address's real IP (see
        _resolve_ip), not its hostname -- so ring placement doesn't depend on
        naming. Used for all node-identity/ring-topology hashing."""
        if not addr: return 0
        host, _, port = addr.partition(":")
        ip = _resolve_ip(host)
        canonical = f"{ip}:{port}" if port else ip
        return int(hashlib.sha1(canonical.encode('utf-8')).hexdigest(), 16) % (2**self.m)

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

    def ping(self) -> bool: return True
    def get_replication_factor(self) -> int: return self.rf
    def get_successor_list(self) -> List[str]: return self.successors
    def get_successor(self) -> str: return self.successor
    def get_predecessor(self) -> Optional[str]: return self.predecessor

    async def find_successor(self, id_str: str) -> str:
        id_val = int(id_str)
        if in_half_open_range(id_val, self.node_id, self.successor_id):
            return self.successor
        else:
            n0_addr = self.closest_preceding_node(id_val)
            if n0_addr == self.address: return self.successor
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
            if n0_addr == self.address: return [self.successor, hop_count]
            try:
                return await self._rpc(n0_addr, "find_successor_with_hops", str(id_val), hop_count + 1)
            except Exception:
                return [self.successor, hop_count]

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

    def store_replica(self, key_id_str: str, value: str) -> bool:
        k_str = str(key_id_str)
        if k_str not in self.storage:
            self.storage[k_str] = []
        try:
            new_id = json.loads(value).get("course_id")
            self.storage[k_str] = [c for c in self.storage[k_str] if json.loads(c).get("course_id") != new_id]
        except Exception:
            pass
        self.storage[k_str].append(value)
        self._replica_sync_dirty = True
        return True

    async def store_local(self, key_id_str: str, value: str) -> bool:
        k_str = str(key_id_str)
        success = self.store_replica(k_str, value)
        if self.rf > 0 and success and self.successor != self.address:
            try:
                await self._rpc(self.successor, "store_replica", k_str, value)
            except Exception:
                pass
        return success

    def store_bulk(self, items: List[Any]) -> int:
        """Batched replica/bulk-load storage: items is a list of [key_str, course_json].
        Mirrors clustered_dht/semantic_router's store_bulk so sync_replicas_to_successors
        and offline bulk loading can send one RPC instead of one per course."""
        n = 0
        for k_str, cjson in items:
            if self.store_replica(str(k_str), cjson):
                n += 1
        return n

    async def sync_replicas_to_successors(self, force: bool = False):
        """Pushes all primary data from this node to its successor list as replicas.
        Batched into one store_bulk RPC per successor per round instead of one
        store_replica RPC per course -- at real corpus scale (tens of thousands
        of courses per node) the unbatched form re-sent every stabilize_interval_sec
        would mean tens of thousands of RPC round-trips per second per node.

        Still O(primary set size) per call, so the routine periodic tick only
        runs it when primary data actually changed since the last sync
        (self._replica_sync_dirty). stabilize() passes force=True whenever the
        successor list changed this round, since a new successor may not have
        any data yet regardless of whether this node's own primary set changed."""
        if not force and not self._replica_sync_dirty:
            return
        if self.rf <= 0 or self.successor == self.address: return
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_addr_hash(pred_addr)
        items = []
        for k_str, courses in list(self.storage.items()):
            key_id = int(k_str)
            if in_half_open_range(key_id, pred_id, self.node_id):
                for course_str in courses:
                    items.append([k_str, course_str])
        if not items:
            self._replica_sync_dirty = False
            return
        had_failure = False
        for succ in self.successors[:self.rf]:
            if succ == self.address: continue
            try:
                await self._rpc(succ, "store_bulk", items)
            except Exception:
                had_failure = True
        self._replica_sync_dirty = had_failure

    def claim_and_migrate_data(self, new_node_id_str: str, new_node_address: str) -> Dict[str, List[str]]:
        new_node_id = int(new_node_id_str)
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_addr_hash(pred_addr)
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
        pred_id = self.get_addr_hash(self.predecessor) if self.predecessor else self.node_id

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

    async def join(self, bootstrap_addr: Optional[str]) -> bool:
        if bootstrap_addr:
            try:
                try:
                    self.r = await self._rpc(bootstrap_addr, "get_replication_factor")
                    self.rf = self.r
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
                    try:
                        migrated_data = await self._rpc(self.successor, "claim_and_migrate_data", str(self.node_id), self.address)
                        for k_str, courses in migrated_data.items():
                            self.storage[k_str] = courses
                    except Exception:
                        pass
                return True
            except Exception:
                return False
        else:
            self.successors = [self.address]
            self.finger_table[0] = self.address
            self.predecessor = None
            return True

    async def put_course(self, course_json: str) -> bool:
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"
        course["vector"] = self._vectorize(text)
        course_json_with_vec = json.dumps(course)

        # Naive: hash the course_id instead of semantic clustering
        course_hash = self.get_hash(course['course_id'])
        target_node = await self.find_successor(str(course_hash))

        if target_node == self.address:
            return await self.store_local(str(course_hash), course_json_with_vec)
        else:
            try:
                return await self._rpc(target_node, "store_local", str(course_hash), course_json_with_vec)
            except Exception:
                return False

    def local_search_ring(self, query_vec: List[float], top_k: int = 5) -> Tuple[List[Tuple[float, str]], str]:
        # Calculate cosine similarity ONLY for PRIMARY courses to avoid duplicate processing in the ring
        results = []
        pred_addr = self.predecessor if self.predecessor else self.address
        pred_id = self.get_addr_hash(pred_addr)

        for k_str, courses in self.storage.items():
            key_id = int(k_str)
            if in_half_open_range(key_id, pred_id, self.node_id) or self.predecessor is None:
                for c_str in courses:
                    c_data = json.loads(c_str)
                    if "vector" in c_data:
                        c_vec = c_data["vector"]
                    else:
                        text = f"{c_data['course_title']} {c_data['category']} {c_data['description']}"
                        c_vec = self._vectorize(text)
                    sim = self._cosine_similarity(query_vec, c_vec)
                    results.append((sim, c_str))

        results.sort(key=lambda x: x[0], reverse=True)

        # Return local results and the exact NEXT node in the ring (successor)
        return results[:top_k], self.successor

    async def get_similar_courses(self, course_json: str, nprobe: int = 1, return_hops: bool = False) -> Any:
        # Naive GET requires traversing the ENTIRE ring sequentially to guarantee 100% recall
        course = json.loads(course_json)
        text = f"{course['course_title']} {course['category']} {course['description']}"
        query_vec = self._vectorize(text)

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
                    local_top, next_node = await self._rpc(current, "local_search_ring", query_vec, 5)
                    print(f"    -> [Crawler] Node {current} (Successor: {next_node}) searched via RPC.")

                titles_with_sim = [f"'{json.loads(c_str)['course_title']}' ({sim:.4f})" for sim, c_str in local_top]
                print(f"       Found local courses: {titles_with_sim}")

                all_results.extend(local_top)

                if not next_node or next_node == current:
                    break

                current = next_node
            except Exception:
                break

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

        cleaned_list = []
        for sim, c_str in final_list[:5]:
            c = json.loads(c_str)
            if "vector" in c:
                del c["vector"]
            cleaned_list.append(json.dumps(c))

        if return_hops:
            return cleaned_list, network_hops
        return cleaned_list

    async def stabilize(self):
        if not self.successors: return
        prev_successors = list(self.successors)
        alive_successors = []
        for succ in self.successors:
            if succ == self.address:
                alive_successors.append(succ)
                continue
            try:
                await self._rpc(succ, "ping")
                alive_successors.append(succ)
            except Exception as e:
                print(f"[{self.address}] Failed to ping successor {succ}: {e}")
        if not alive_successors:
            self.successors = [self.address]
        else:
            self.successors = alive_successors
            succ = self.successors[0]

            if succ == self.address:
                if self.predecessor and self.predecessor != self.address:
                    try:
                        await self._rpc(self.predecessor, "ping")
                        self.successors = [self.predecessor]
                        succ = self.predecessor
                    except Exception as e:
                        print(f"[{self.address}] Failed to ping predecessor {self.predecessor}: {e}")
                        self.predecessor = None

            if succ != self.address:
                try:
                    x = await self._rpc(succ, "get_predecessor")
                    if x and x != self.address:
                        x_id = self.get_addr_hash(x)
                        if in_open_range(x_id, self.node_id, self.get_addr_hash(succ)):
                            try:
                                await self._rpc(x, "ping")
                                self.successors.insert(0, x)
                                self.successors = self.successors[:self.r]
                                succ = x
                            except Exception as e:
                                print(f"[{self.address}] Failed to ping candidate successor {x}: {e}")
                    await self._rpc(succ, "notify", self.address)

                    try:
                        s_list = await self._rpc(succ, "get_successor_list")
                        new_list = [succ]
                        for n in s_list:
                            if n not in new_list and n != self.address:
                                new_list.append(n)
                        self.successors = new_list[:self.r]
                    except Exception as e:
                        print(f"[{self.address}] Failed to fetch successor list from {succ}: {e}")
                except Exception as e:
                    print(f"[{self.address}] Error during stabilization communication with successor {succ}: {e}")
        await self.cleanup_stale_replicas()
        await self.sync_replicas_to_successors(force=(self.successors != prev_successors))

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
                key_id = int(k_str)
                if not in_half_open_range(key_id, limit_id, self.node_id) and self.predecessor is not None:
                    keys_to_delete.append(k_str)
            for k in keys_to_delete:
                del self.storage[k]

    async def fix_fingers(self):
        import random
        i = random.randint(0, self.m - 1)
        target_id = (self.node_id + 2**i) % (2**self.m)
        try:
            self.finger_table[i] = await self.find_successor(str(target_id))
        except Exception:
            pass

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
            except Exception:
                pass
            try:
                await self.fix_fingers()
            except Exception:
                pass
            try:
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
