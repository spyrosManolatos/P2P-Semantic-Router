import os
import sys
import json
import random
import argparse
import xmlrpc.client
import socket
import time
import concurrent.futures
import hashlib

socket.setdefaulttimeout(30)

# Add src to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from benchmarks.metrics import compute_recall, timer_decorator
from core.config_loader import load_config
from core.json_rpc_client import JSONRPCProxy
from architectures.monolithic_linear.linear_search import MonolithicSearcher


def rpc_client(address: str, timeout: float = 30.0):
    """Returns an xmlrpc.client.ServerProxy for the xmlrpc-based architectures,
    or a JSONRPCProxy (talking to the FastAPI/httpx node) for the async_* ones --
    selected by the "a" (async container hostname prefix: "async-"/"av-") so
    existing call sites for the other architectures stay untouched."""
    host = address.split(":")[0]
    if host.startswith("async-") or host.startswith("av-"):
        return JSONRPCProxy(address, timeout=timeout)
    return xmlrpc.client.ServerProxy(f"http://{address}", allow_none=True)


def base_arch(arch: str) -> str:
    """Strips the async_ prefix so behavior conditionals (nprobe use, standard-vs-
    clustered-vs-semantic branching) don't need to be duplicated per transport."""
    return arch[len("async_"):] if arch.startswith("async_") else arch

@timer_decorator
def run_monolithic(searcher, query_text):
    return searcher.search(query_text, top_k=5)

@timer_decorator
def run_dht_query(node_rpc, course_json, nprobe=None):
    if nprobe is None:
        return node_rpc.get_similar_courses(course_json, 1, True)
    else:
        return node_rpc.get_similar_courses(course_json, nprobe, True)

def run_query_batch(node_rpc, ground_truth, nprobe, concurrency=1, use_nprobe=True):
    """Runs every ground-truth query against `node_rpc`, at most `concurrency`
    in flight, and returns a list ALIGNED TO ground_truth ORDER holding either
    the raw run_dht_query return value or the Exception that was raised.

    Order preservation is the point: the sequential loop this replaces builds
    per-query recall lists positionally (the region/concentration analysis
    indexes straight back into ground_truth), so completion order must not leak
    into the results. concurrency=1 keeps the original one-at-a-time behaviour
    exactly, which is what every non-sweep caller still uses.

    Recall and hops are both computed server-side per query and do not depend on
    how many queries are in flight, so concurrency changes only wall-clock -- NOT
    the measurement. The one real risk is saturating the ring hard enough that
    queries hit the RPC timeout and get counted as failures; the n=<completed>/
    <total> counter printed by each sweep line is what exposes that."""
    def _one(gt):
        return run_dht_query(node_rpc, json.dumps(gt["course"]),
                             nprobe if use_nprobe else None)

    outcomes = [None] * len(ground_truth)
    if concurrency <= 1:
        for i, gt in enumerate(ground_truth):
            try:
                outcomes[i] = _one(gt)
            except Exception as exc:
                outcomes[i] = exc
        return outcomes

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {pool.submit(_one, gt): i for i, gt in enumerate(ground_truth)}
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            try:
                outcomes[i] = fut.result()
            except Exception as exc:
                outcomes[i] = exc
    return outcomes


def inject_data_simple(node_address, courses, arch_name):
    print(f"Injecting {len(courses)} courses into {arch_name} cluster via {node_address}...")
    client = rpc_client(node_address)
    for i, c in enumerate(courses):
        try:
            client.put_course(json.dumps(c))
        except Exception as e:
            print(f"  Failed to inject course {i}: {e}")
        if (i+1) % 100 == 0:
            print(f"  {i+1}/{len(courses)} injected...")
    print("Injection complete.\n")

def get_node_addresses(arch):
    if arch == "standard":
        return [
            "standard-bootstrap:5000",
            "standard-node-1:5000",
            "standard-node-2:5000",
            "standard-node-3:5000",
            "standard-node-4:5000"
        ]
    elif arch == "clustered":
        return [
            "clustered-bootstrap:5000",
            "clustered-node-1:5000",
            "clustered-node-2:5000",
            "clustered-node-3:5000",
            "clustered-node-4:5000"
        ]
    elif arch == "async_standard":
        return [
            "async-standard-bootstrap:5000",
            "async-standard-node-1:5000",
            "async-standard-node-2:5000",
            "async-standard-node-3:5000",
            "async-standard-node-4:5000"
        ]
    elif arch == "async_clustered":
        return [
            "async-clustered-bootstrap:5000",
            "async-clustered-node-1:5000",
            "async-clustered-node-2:5000",
            "async-clustered-node-3:5000",
            "async-clustered-node-4:5000"
        ]
    elif arch == "async_semantic":
        return [
            "async-bootstrap-node:5000",
            "async-node-1:5000",
            "async-node-2:5000",
            "async-node-3:5000",
            "async-node-4:5000"
        ]
    else: # semantic
        return [
            "bootstrap-node:5000",
            "node-1:5000",
            "node-2:5000",
            "node-3:5000",
            "node-4:5000"
        ]

def arch_label_for(arch: str) -> str:
    base = base_arch(arch)
    label = ("Standard Chord DHT" if base == "standard"
             else "Clustered Chord DHT" if base == "clustered"
             else "Semantic Router Chord DHT")
    return f"Async {label} (FastAPI/httpx)" if arch.startswith("async_") else f"{label} (XML-RPC)"


def run_scaling(args, subset_courses, ground_truth):
    arch_label = arch_label_for(args.arch)
    print(f"\n=== Running {arch_label} Scaling & Recall Benchmark ===")
    
    node_addresses = get_node_addresses(args.arch)
    
    # Inject data into cluster
    bulk_inject(args, node_addresses, subset_courses)
    
    # Wait for replication to settle
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)
    
    benchmark_results = {
        "monolithic": [],
        "dht_results": {}
    }
    
    # 1. Monolithic benchmark
    searcher = MonolithicSearcher(dataset=args.dataset, limit=len(subset_courses))
    for gt in ground_truth:
        _, latency = run_monolithic(searcher, gt["query"])
        benchmark_results["monolithic"].append(latency)
        
    print(f"Monolithic: Mean Latency = {sum(benchmark_results['monolithic'])/len(benchmark_results['monolithic']):.2f}ms")
    
    # 2. DHT benchmark
    use_nprobe = (base_arch(args.arch) in ["clustered", "semantic"])
    if use_nprobe:
        HashNode = dummy_node_class(args.arch)
        k = HashNode("127.0.0.1", 5000).k
        nprobe_values = [n for n in [1, 2, 3, 5, 8, 12, 20, 40, 80] if n <= max(1, k)]
    else:
        nprobe_values = [1]
    
    for np in nprobe_values:
        np_key = f"nprobe_{np}" if use_nprobe else "standard"
        benchmark_results["dht_results"][np_key] = {
            "recall": [],
            "hops": [],
            "latency": []
        }
        
        print(f"Evaluating {args.arch} with nprobe={np if use_nprobe else 'None'}...")
        # Use a fixed gateway node to eliminate random routing variance
        client = rpc_client(node_addresses[0])
        
        for gt in ground_truth:
            c_json = json.dumps(gt["course"])
            try:
                (res_tuple, hops), latency = run_dht_query(client, c_json, np if use_nprobe else None)
                retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                recall = compute_recall(retrieved_ids, gt["ground_truth_ids"])
                
                benchmark_results["dht_results"][np_key]["recall"].append(recall)
                benchmark_results["dht_results"][np_key]["hops"].append(hops)
                benchmark_results["dht_results"][np_key]["latency"].append(latency)
            except Exception as e:
                print(f"  Query failed: {e}")
                
        r_mean = sum(benchmark_results["dht_results"][np_key]["recall"]) / len(ground_truth)
        h_mean = sum(benchmark_results["dht_results"][np_key]["hops"]) / len(ground_truth)
        l_mean = sum(benchmark_results["dht_results"][np_key]["latency"]) / len(ground_truth)
        print(f"  Result -> Recall: {r_mean*100:.1f}%, Hops: {h_mean:.2f}, Latency: {l_mean:.2f}ms")

    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized", f"results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(benchmark_results, f, indent=4)
    print(f"Scaling results saved to {out_path}")

def run_characterization(args, subset_courses, ground_truth):
    """Injects the dataset, then queries every node's live get_info() to record the
    ACTUAL ring state: which clusters hold data (occupancy) and how the load is
    distributed across physical nodes. Measured from the running ring, not simulated."""
    print(f"\n=== Running {args.arch} Ring Characterization ===")
    node_addresses = get_node_addresses(args.arch)

    bulk_inject(args, node_addresses, subset_courses)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)

    cluster_totals = {}     # cluster_id -> total primary course count across the ring
    per_node_primary = {}   # node address -> primary course count
    for addr in node_addresses:
        try:
            info = rpc_client(addr).get_info()
        except Exception as e:
            print(f"  Could not reach {addr}: {e}")
            continue
        # Clustered/Semantic expose a per-cluster primary_summary; Standard only a count.
        if "primary_summary" in info:
            node_primary = 0
            for cid, cnt in info["primary_summary"].items():
                cluster_totals[cid] = cluster_totals.get(cid, 0) + cnt
                node_primary += cnt
            per_node_primary[addr] = node_primary
        else:
            per_node_primary[addr] = info.get("primary_count", 0)

    sizes = sorted(cluster_totals.values(), reverse=True)
    result = {
        "dataset_size": args.dataset_size,
        "clusters_populated": len(cluster_totals),
        "cluster_sizes": sizes,
        "largest_cluster": sizes[0] if sizes else 0,
        "median_cluster": sizes[len(sizes) // 2] if sizes else 0,
        "per_node_primary": per_node_primary,
    }
    print(f"Clusters populated: {len(cluster_totals)} | largest {result['largest_cluster']} "
          f"| median {result['median_cluster']}")
    print(f"Per-node primary distribution: {per_node_primary}")

    out_path = os.path.join(project_root(), "data", "benchmarks", "results",
                            "containerized", f"ring_characterization_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=4)
    print(f"Ring characterization saved to {out_path}")


def run_fault_tolerance(args, subset_courses, ground_truth):
    arch_label = arch_label_for(args.arch)
    print(f"\n=== Running {arch_label} Fault Tolerance Benchmark ===")
    
    node_addresses = get_node_addresses(args.arch)
    fault_results = {}

    # Inject data (each experiment runs on its own fresh ring, so it must
    # populate the ring itself rather than rely on a previous experiment).
    bulk_inject(args, node_addresses, subset_courses)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)

    # Measure baseline latency and recall
    client = rpc_client(node_addresses[0])
    baseline_latencies = []
    baseline_recalls = []
    baseline_hops = []

    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(client, c_json, args.nprobe if base_arch(args.arch) != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(retrieved_ids, gt["ground_truth_ids"])
            baseline_recalls.append(recall)
            baseline_latencies.append(latency)
            baseline_hops.append(hops)
        except Exception:
            pass

    mean_baseline_latency = sum(baseline_latencies) / len(baseline_latencies) if baseline_latencies else 0
    mean_baseline_recall = sum(baseline_recalls) / len(baseline_recalls) if baseline_recalls else 0
    mean_baseline_hops = sum(baseline_hops) / len(baseline_hops) if baseline_hops else 0

    print(f"Baseline Mean Latency: {mean_baseline_latency:.2f}ms")
    print(f"Baseline Mean Recall: {mean_baseline_recall*100:.1f}%")
    print(f"Baseline Mean Hops: {mean_baseline_hops:.2f}")

    fault_results["baseline"] = {
        "recall": mean_baseline_recall,
        "hops": mean_baseline_hops,
        "latency": mean_baseline_latency,
        "heal_time_sec": 0.0
    }
    
    # Kill the second node gracefully via RPC stop
    failed_node_addr = node_addresses[1]
    print(f"Sending shutdown RPC to {failed_node_addr}...")
    kill_client = rpc_client(failed_node_addr)
    try:
        kill_client.stop()
    except Exception:
        pass
        
    # Measure recovery time
    surviving_nodes = [addr for addr in node_addresses if addr != failed_node_addr]
    start_heal = time.time()
    healed = False
    
    print("Waiting for Chord ring stabilization and self-healing...")
    for attempt in range(25):
        time.sleep(1)
        try:
            test_client = rpc_client(surviving_nodes[0])
            info = test_client.get_info()
            if info["successor"] != failed_node_addr:
                healed = True
                break
        except Exception:
            pass
            
    heal_duration = time.time() - start_heal
    print(f"Successor pointer repaired in {heal_duration:.2f} seconds.")

    active_client = rpc_client(surviving_nodes[0])

    def measure_state():
        """Runs the full query set against the surviving ring; returns mean recall/latency/hops."""
        recalls, latencies, hops_list = [], [], []
        for gt in ground_truth:
            c_json = json.dumps(gt["course"])
            try:
                (res_tuple, hops), latency = run_dht_query(active_client, c_json, args.nprobe if base_arch(args.arch) != "standard" else None)
                retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                recalls.append(compute_recall(retrieved_ids, gt["ground_truth_ids"]))
                latencies.append(latency)
                hops_list.append(hops)
            except Exception:
                pass
        n = len(recalls) if recalls else 1
        return {
            "recall": sum(recalls) / n if recalls else 0,
            "latency": sum(latencies) / len(latencies) if latencies else 0,
            "hops": sum(hops_list) / len(hops_list) if hops_list else 0,
        }

    # --- Two-point measurement ---------------------------------------------
    # (1) TRANSIENT: shortly after the failure, before replica promotion and
    #     finger repair complete. Captures the recovery-window dip and the
    #     failed-finger timeout latency spike.
    # (2) HEALED: after full stabilization. With RF>=2 the successor has
    #     promoted its replica, so recall should recover toward baseline,
    #     demonstrating that a single failure is fully recoverable.
    TRANSIENT_WAIT_SEC = 5
    HEALED_WAIT_SEC = 20   # additional wait AFTER the transient measurement

    print(f"Waiting {TRANSIENT_WAIT_SEC}s, then measuring TRANSIENT state...")
    time.sleep(TRANSIENT_WAIT_SEC)
    transient = measure_state()
    transient["heal_time_sec"] = heal_duration
    print(f"  Transient: recall={transient['recall']:.3f}  latency={transient['latency']:.0f}ms")

    print(f"Waiting a further {HEALED_WAIT_SEC}s for full replica promotion, then measuring HEALED state...")
    time.sleep(HEALED_WAIT_SEC)
    healed = measure_state()
    print(f"  Healed: recall={healed['recall']:.3f}  latency={healed['latency']:.0f}ms")

    fault_results["after_failure_transient"] = transient
    fault_results["after_failure_healed"] = healed
    # Backwards-compatible alias: existing plots read "after_failure" (use transient).
    fault_results["after_failure"] = {**transient}
    fault_results["nprobe"] = args.nprobe if base_arch(args.arch) != "standard" else 1

    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized", f"fault_tolerance_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(fault_results, f, indent=4)
    print(f"Fault tolerance results saved to {out_path}")

def node5_host(arch: str) -> str:
    """Hostname of the dedicated 6th node container, idle until run_node_join joins it."""
    base = base_arch(arch)
    prefix = "async-" if arch.startswith("async_") else ""
    if base == "standard":
        return f"{prefix}standard-node-5" if prefix else "standard-node-5"
    elif base == "clustered":
        return f"{prefix}clustered-node-5" if prefix else "clustered-node-5"
    else:
        return f"{prefix}node-5" if prefix else "node-5"


def run_node_join(args, subset_courses, ground_truth):
    arch_label = arch_label_for(args.arch)
    print(f"\n=== Running {arch_label} Node Join Benchmark ===")

    node_addresses = get_node_addresses(args.arch)

    # 1. Inject data
    bulk_inject(args, node_addresses, subset_courses)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)

    # Target the dedicated 6th node container running idle in the docker network
    target_host = node5_host(args.arch)
    target_addr = f"{target_host}:5000"
    
    print(f"Connecting to idle container {target_host}...")
    new_node = rpc_client(target_addr)
    existing_node = rpc_client(node_addresses[0])
    
    # --- QUERY BEFORE JOIN ---
    print("Executing queries on the original 5-node ring...")
    pre_latencies = []
    pre_recalls = []
    pre_hops = []
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(existing_node, c_json, args.nprobe if base_arch(args.arch) != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(retrieved_ids, gt["ground_truth_ids"])
            pre_recalls.append(recall)
            pre_latencies.append(latency)
            pre_hops.append(hops)
        except Exception:
            pass

    mean_pre_recall = sum(pre_recalls) / len(pre_recalls) if pre_recalls else 0
    mean_pre_latency = sum(pre_latencies) / len(pre_latencies) if pre_latencies else 0
    mean_pre_hops = sum(pre_hops) / len(pre_hops) if pre_hops else 0
    
    # --- TRIGGER JOIN ---
    
    start_time = time.time()
    print(f"Triggering {target_host} to join the cluster via bootstrap node {node_addresses[0]}...")
    new_node.join(node_addresses[0])
    
    STABILIZATION_WINDOW = 10.0  # fixed wait for stabilization; NOT a measured migration cost
    print(f"Waiting {STABILIZATION_WINDOW}s (fixed stabilization window) for data migration...")
    time.sleep(STABILIZATION_WINDOW)

    heal_duration = time.time() - start_time
    
    # Verify migration
    info = new_node.get_info()
    print(f"Joined node status: {info}")
    primary_count = 0
    if "primary_summary" in info:
        primary_count = sum(info["primary_summary"].values())
    else:
        primary_count = info.get("primary_count", info.get("keys_stored", 0))
        
    print(f"Joined node migrated {primary_count} primary courses from the cluster.")
    
    # Query from new node
    post_latencies = []
    post_recalls = []
    post_hops = []
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(new_node, c_json, args.nprobe if base_arch(args.arch) != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(retrieved_ids, gt["ground_truth_ids"])
            post_recalls.append(recall)
            post_latencies.append(latency)
            post_hops.append(hops)
        except Exception as e:
            print(f"  Query failed: {e}")

    mean_recall = sum(post_recalls) / len(post_recalls) if post_recalls else 0
    mean_latency = sum(post_latencies) / len(post_latencies) if post_latencies else 0
    mean_post_hops = sum(post_hops) / len(post_hops) if post_hops else 0

    join_results = {
        "nprobe": args.nprobe if base_arch(args.arch) != "standard" else 1,
        "original_5_nodes": {
            "recall": mean_pre_recall,
            "hops": mean_pre_hops,
            "latency": mean_pre_latency,
        },
        "expanded_6_nodes": {
            "recall": mean_recall,
            "hops": mean_post_hops,
            "latency": mean_latency,
            "migrated_primary_courses": primary_count,
        },
        # Fixed stabilization window, not an independently measured migration time.
        "stabilization_window_sec": STABILIZATION_WINDOW,
    }
    
    # Terminate the server inside the remote container so it shuts down and stays idle for the next runs
    try:
        new_node.stop()
    except Exception:
        pass
    
    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized", f"node_join_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(join_results, f, indent=4)
    print(f"Node join results saved to {out_path}")

def project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def queries_file_path():
    return os.path.join(project_root(), "data", "benchmarks", "queries", "queries_500.json")


def generate_queries(args):
    """Pre-generate a fixed queries.json shared across all architectures for fair comparison."""
    print("\n=== Generating Fixed Query Set ===")
    searcher = MonolithicSearcher(dataset=args.dataset, limit=args.dataset_size)
    subset_courses = searcher.courses

    random.seed(42)
    test_courses = random.sample(subset_courses, min(args.queries, len(subset_courses)))

    ground_truth = []
    print(f"Precomputing monolithic ground truth for {len(test_courses)} fixed queries...")
    for tc in test_courses:
        q_text = f"{tc['course_title']} {tc['category']} {tc['description']}"
        res, _ = run_monolithic(searcher, q_text)
        gt_ids = [res_course['course_id'] for sim, res_course in res]
        ground_truth.append({
            "query": q_text,
            "course": tc,
            "ground_truth_ids": gt_ids
        })
        print(f"  [{tc['course_id']}] {tc['course_title'][:60]} -> GT: {gt_ids}")

    out_path = queries_file_path()
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        json.dump({"dataset": args.dataset, "dataset_size": args.dataset_size, "ground_truth": ground_truth}, f, indent=4)
    print(f"\nFixed query set saved to {out_path}")
    print("All architecture benchmarks will now use this identical query set.")


def load_ground_truth(args, searcher):
    """Load fixed queries.json if it exists, otherwise fall back to random sampling with a warning."""
    qpath = getattr(args, "queries_file", None)
    if qpath:
        # Explicit override (e.g. the full-corpus GT for the scaling experiment).
        qpath = os.path.join(project_root(), qpath) if not os.path.isabs(qpath) else qpath
    else:
        qpath = queries_file_path()
    if os.path.exists(qpath):
        print(f"Loading fixed query set from {qpath} ...")
        with open(qpath, "r") as f:
            data = json.load(f)
        ground_truth = data["ground_truth"]
        print(f"Loaded {len(ground_truth)} fixed queries (dataset={data['dataset']}, size={data['dataset_size']}).")
        return ground_truth
    else:
        print("WARNING: No fixed queries.json found. Falling back to random query sampling.")
        print("         Run with --gen-queries first to ensure fair cross-architecture comparison!")
        test_courses = random.sample(searcher.courses, min(args.queries, len(searcher.courses)))
        ground_truth = []
        print("Precomputing monolithic ground truth for search queries...")
        for tc in test_courses:
            q_text = f"{tc['course_title']} {tc['category']} {tc['description']}"
            res, _ = run_monolithic(searcher, q_text)
            gt_ids = [res_course['course_id'] for sim, res_course in res]
            ground_truth.append({"query": q_text, "course": tc, "ground_truth_ids": gt_ids})
        return ground_truth


def dummy_node_class(arch: str):
    """Imports the (unstarted) node class matching `arch`, used purely for its
    local vectorization/hashing math (_vectorize_and_find_centroids, get_cluster_hash,
    get_hash) -- no network, no server started."""
    base = base_arch(arch)
    is_async = arch.startswith("async_")
    if base == "clustered":
        if is_async:
            from architectures.async_clustered_dht.node import ChordNode as HashNode
        else:
            from architectures.clustered_dht.node import ChordNode as HashNode
    else:  # semantic
        if is_async:
            from architectures.async_semantic_router.node import ChordNode as HashNode
        else:
            from architectures.semantic_router.node import ChordNode as HashNode
    return HashNode


def run_disaster_scenario(args, subset_courses, ground_truth):
    if base_arch(args.arch) == "standard":
        print("Skipping disaster scenario for Standard DHT (nprobe logic does not apply).")
        return

    print(f"\n=== Running {args.arch} Disaster Scenario ===")

    node_addresses = get_node_addresses(args.arch)
    client = rpc_client(node_addresses[0])

    # 1. Inject data
    bulk_inject(args, node_addresses, subset_courses)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)

    # 2. Pick a single target query and find its primary cluster
    test_query = ground_truth[0]
    q_text = test_query["query"]

    HashNode = dummy_node_class(args.arch)
    dummy_node = HashNode("127.0.0.1", 5000)

    print(f"Target query: {q_text[:50]}...")

    # Locate the node owning this query's primary cluster, using the SAME hashing
    # the real nodes use (get_cluster_hash), so we assassinate the true owner.
    top_cluster_id = dummy_node._vectorize_and_find_centroids(q_text, 1)[0]
    cluster_hash = dummy_node.get_cluster_hash(top_cluster_id)
    target_node = client.find_successor(str(cluster_hash))
    print(f"Top cluster ID is {top_cluster_id} (hash: {cluster_hash}).")
    print(f"Primary owner of this cluster is: {target_node}")

    # Measure Baseline Recall
    baseline_recalls = []
    print("Running Baseline Queries...")
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, _), _ = run_dht_query(client, c_json, 5) # High nprobe=5
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(retrieved_ids, gt["ground_truth_ids"])
            baseline_recalls.append(recall)
        except Exception:
            pass

    mean_baseline = sum(baseline_recalls) / len(baseline_recalls) if baseline_recalls else 0
    print(f"Baseline Mean Recall (nprobe=5): {mean_baseline*100:.1f}%")

    # 3. Trigger a CORRELATED failure: kill the primary owner AND its ring
    #    successors (where RF replicas live). Adjacent ring nodes model a
    #    rack/AZ/power-domain outage. With RF replicas colocated on the successor
    #    chain, killing (RF+1) consecutive nodes wipes a contiguous region ENTIRELY.
    #    In the Semantic Router that region is semantically contiguous (a whole
    #    topic disappears); under SHA-1 scatter the same kills only remove a
    #    random smattering of clusters, so recall largely survives.
    kills = max(1, min(args.disaster_kills, len(node_addresses) - 1))
    to_kill = [target_node]
    walker = rpc_client(target_node)
    for _ in range(kills - 1):
        try:
            succ = walker.get_successor()
        except Exception:
            break
        if not succ or succ in to_kill:
            break
        to_kill.append(succ)
        walker = rpc_client(succ)

    print(f"\nTriggering CORRELATED Disaster: killing {len(to_kill)} adjacent nodes: {to_kill}")
    for addr in to_kill:
        try:
            rpc_client(addr).stop()
        except Exception:
            pass

    surviving_nodes = [addr for addr in node_addresses if addr not in to_kill]
    if not surviving_nodes:
        print("All nodes killed; aborting disaster measurement.")
        return
    surviving_client = rpc_client(surviving_nodes[0])

    # 4. Let the ring re-stabilize so routing recovers around the dead nodes.
    #    This isolates genuine DATA LOSS from transient routing exceptions:
    #    whatever recall is lost now is data that no surviving replica holds.
    print("Waiting 15s for the ring to route around the failure (isolating data loss)...")
    time.sleep(15)

    print("Executing queries under disaster conditions...")
    disaster_recalls = []
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, _), _ = run_dht_query(surviving_client, c_json, 5) # High nprobe=5
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(retrieved_ids, gt["ground_truth_ids"])
            disaster_recalls.append(recall)
        except Exception:
            disaster_recalls.append(0.0)

    mean_disaster = sum(disaster_recalls) / len(disaster_recalls) if disaster_recalls else 0
    print(f"Disaster Mean Recall (nprobe=5, {len(to_kill)} correlated kills): {mean_disaster*100:.1f}%")
    
    disaster_results = {
        "baseline_recall": mean_baseline,
        "disaster_recall": mean_disaster,
        "correlated_kills": len(to_kill),
        "surviving_nodes": len(surviving_nodes)
    }
    
    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized", f"disaster_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(disaster_results, f, indent=4)
    print(f"Disaster results saved to {out_path}")




def get_vnode_addresses(args):
    """The full list of virtual-node addresses for the scaling ring:
    each container hosts vnodes_per_container identities on ports 5000..5000+n-1."""
    containers = [c.strip() for c in args.containers.split(",") if c.strip()]
    addrs = []
    for c in containers:
        for i in range(args.vnodes_per_container):
            addrs.append(f"{c}:{5000 + i}")
    return addrs


def run_vnode_disaster_scenario(args, subset_courses, ground_truth):
    """Same correlated-failure mechanic as run_disaster_scenario, but on the
    large virtual-node ring (get_vnode_addresses) instead of the fixed 5-node
    ring. Tests whether semantic's disaster-recall disadvantage (a contiguous
    topic region wiped out) narrows at finer ring granularity: at N=100, each
    node/vnode owns a much smaller slice of the corpus, so killing RF+1
    adjacent nodes destroys a much smaller fraction of the ring than at N=5.

    Like every other experiment, this KEEPS replication enabled (config.vnodes.yaml
    default RF=2) since the whole mechanic depends on replicas existing to be
    wiped out."""
    if base_arch(args.arch) == "standard":
        print("Skipping vnode disaster scenario for Standard DHT (nprobe logic does not apply).")
        return

    print(f"\n=== Running {args.arch} Virtual-Node Disaster Scenario ===")

    node_addresses = get_vnode_addresses(args)
    print(f"Ring: {len(node_addresses)} virtual nodes across {args.containers} "
          f"({args.vnodes_per_container}/container).")

    if not wait_for_ring_convergence(node_addresses, timeout=args.converge_timeout):
        return

    client = rpc_client(node_addresses[0], timeout=getattr(args, "query_timeout", 30.0))

    # Routing is verified HERE -- after the cycle closed, before any data is loaded.
    # It used to run as a `docker exec` pre-flight before this process even started,
    # which asked whether a still-forming ring could route and answered "no".
    if not functional_routing_check(client, len(node_addresses),
                                    k_clusters=getattr(args, "k_clusters", 5500)):
        return

    # 1. Inject data (replication stays at config default RF=2 -- no override).
    bulk_inject(args, node_addresses, subset_courses)
    print("Waiting for finger tables to settle before measuring...")
    wait_for_finger_stability(node_addresses, timeout=args.converge_timeout)

    HashNode = dummy_node_class(args.arch)
    dummy_node = HashNode("127.0.0.1", 5000)

    # Tag every query with the cluster it "comes from" (its source course's top
    # cluster). Prefer the value baked into the query file by gen_queries_bigk;
    # fall back to computing it, so older query files still work.
    query_clusters = []
    for gt in ground_truth:
        qc = gt.get("query_cluster")
        if qc is None:
            qc = int(dummy_node._vectorize_and_find_centroids(gt["query"], 1)[0])
        query_clusters.append(int(qc))

    # 2. Epicenter selection. Default: the HEAVIEST primary node (most primary
    #    courses) -- the realistic correlated-failure target (an overloaded hot
    #    node) and the one that maximizes collateral signal. Override with
    #    --disaster_cluster >= 0 to target a specific cluster's owner instead
    #    (reproducible across corpora / for the krylov runs).
    if getattr(args, "disaster_cluster", -1) is not None and args.disaster_cluster >= 0:
        cluster_hash = dummy_node.get_cluster_hash(args.disaster_cluster)
        target_node = client.find_successor(str(cluster_hash))
        print(f"Epicenter: cluster {args.disaster_cluster} -> owner {target_node}.")
    else:
        heaviest, best_load = None, -1
        for addr in node_addresses:
            try:
                info = rpc_client(addr).get_info()
                load = sum(info.get("primary_summary", {}).values())
            except Exception:
                continue
            if load > best_load:
                best_load, heaviest = load, addr
        target_node = heaviest
        print(f"Epicenter: heaviest primary node {target_node} ({best_load} primary courses).")

    # Record the EXACT clusters whose primary copy dies. Killing target + RF
    # successors permanently destroys only target's primary clusters (their
    # replicas live on those successors, which we also kill); every other node's
    # data survives via replicas elsewhere. So target's primary_summary keys ARE
    # the permanently-destroyed set.
    try:
        killed_info = rpc_client(target_node).get_info()
        killed_clusters = set(int(c) for c in killed_info.get("primary_summary", {}))
    except Exception:
        killed_clusters = set()
    print(f"Destroyed cluster set: {len(killed_clusters)} clusters "
          f"(sample {sorted(killed_clusters)[:8]}).")

    # nprobe sweep: does higher fanout recover availability under a correlated
    # failure? Semantic's linear cluster mapping means "adjacent cluster IDs"
    # (what higher nprobe fans out to) are also ring-adjacent -- likely INSIDE
    # the same contiguous region the disaster just wiped. Clustered's SHA-1
    # scatter means the extra probed clusters land on essentially random ring
    # positions -- more likely to hit a surviving node. Which one actually
    # benefits more from higher nprobe under disaster is an open question this
    # sweep answers empirically, not something to assume from the routing story.
    requested = getattr(args, "nprobe_list", "") or ""
    default_sweep = ([int(x) for x in requested.split(",") if x.strip()]
                     if requested.strip() else [1, 5, 20])
    nprobe_values = [n for n in default_sweep if n <= max(1, dummy_node.k)]

    def measure(rpc, label):
        recalls_by_nprobe = []      # mean recall per nprobe
        perq_by_nprobe = []         # per-query recall lists, aligned to ground_truth order
        # Routing hops come back from every query anyway; capturing them here lets
        # one disaster run over a ladder of N answer BOTH open questions on the
        # same rings and the same queries: whether the resilience gap narrows as
        # K/N -> 1, and whether routing cost tracks O(log N + nprobe) against
        # O(nprobe * log N) as N grows. Baseline-vs-disaster hops also show
        # whether a kill inflates routing cost, not just recall.
        hops_by_nprobe = []         # mean hops per nprobe, over COMPLETED queries
        completed_by_nprobe = []    # denominator for the hops mean (see below)
        for npb in nprobe_values:
            recalls, hops_list = [], []
            outcomes = run_query_batch(rpc, ground_truth, npb,
                                       concurrency=getattr(args, "query_concurrency", 1))
            for gt, outcome in zip(ground_truth, outcomes):
                if isinstance(outcome, Exception):
                    # A failed query IS zero recall under disaster, so it counts
                    # in the recall mean. It has no meaningful hop count though,
                    # so it is excluded from the hops mean -- hence the separate
                    # completed_by_nprobe denominator, which keeps that exclusion
                    # visible instead of silently shrinking the divisor.
                    recalls.append(0.0)
                    continue
                (res_tuple, hops), _ = outcome
                retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                recalls.append(compute_recall(retrieved_ids, gt["ground_truth_ids"]))
                hops_list.append(hops)
            mean_r = sum(recalls) / len(recalls) if recalls else 0.0
            mean_h = sum(hops_list) / len(hops_list) if hops_list else 0.0
            recalls_by_nprobe.append(mean_r)
            perq_by_nprobe.append(recalls)
            hops_by_nprobe.append(mean_h)
            completed_by_nprobe.append(len(hops_list))
            flag = "" if len(hops_list) == len(ground_truth) else "  !! PARTIAL"
            print(f"  [{label}] nprobe={npb:2d} -> recall {mean_r*100:5.1f}% | "
                  f"hops {mean_h:6.2f} | n={len(hops_list)}/{len(ground_truth)}{flag}")
        return recalls_by_nprobe, perq_by_nprobe, hops_by_nprobe, completed_by_nprobe

    print("Running Baseline Queries (nprobe sweep)...")
    baseline_recalls, baseline_perq, baseline_hops, baseline_done = measure(client, "baseline")
    # Headline granularity: nprobe=5 (matches the 5-node disaster result) if in
    # the sweep, else the largest nprobe; full curve is in the *_by_nprobe lists.
    headline_idx = nprobe_values.index(5) if 5 in nprobe_values else len(nprobe_values) - 1
    head_np = nprobe_values[headline_idx] if nprobe_values else 0
    mean_baseline = baseline_recalls[headline_idx] if baseline_recalls else 0.0

    # 3. Trigger a CORRELATED failure: kill the primary owner AND its ring
    #    successors (where RF replicas live) -- same RF+1 mechanic as the
    #    5-node disaster scenario, just on a much bigger ring.
    kills = max(1, min(args.disaster_kills, len(node_addresses) - 1))
    to_kill = [target_node]
    walker = rpc_client(target_node)
    for _ in range(kills - 1):
        try:
            succ = walker.get_successor()
        except Exception:
            break
        if not succ or succ in to_kill:
            break
        to_kill.append(succ)
        walker = rpc_client(succ)

    print(f"\nTriggering CORRELATED Disaster: killing {len(to_kill)} adjacent vnodes: {to_kill}")
    for addr in to_kill:
        try:
            rpc_client(addr).stop()
        except Exception:
            pass

    surviving_nodes = [addr for addr in node_addresses if addr not in to_kill]
    if not surviving_nodes:
        print("All nodes killed; aborting disaster measurement.")
        return
    surviving_client = rpc_client(surviving_nodes[0], timeout=getattr(args, "query_timeout", 30.0))

    # A blind fixed sleep isn't enough to guarantee correctness: closest_preceding_node()
    # picks a finger table entry WITHOUT checking liveness, and if that entry is a stale
    # pointer to a now-dead node, find_successor's exception fallback just returns this
    # node's own successor as a best-effort guess -- not necessarily the true owner. That
    # self-healing (check_predecessor/stabilize/fix_fingers) is gradual, a few finger slots
    # per ~1s tick, so a fixed 15s sleep on a 100-node ring risks measuring "disaster recall"
    # that's contaminated by transient wrong-answer routing, not just genuine data loss.
    # Re-run the same rigorous convergence gates used for initial ring setup, against the
    # surviving nodes only, so we measure the true post-healing state.
    print("Waiting for the surviving ring to heal around the failure (isolating data loss)...")
    if not wait_for_ring_convergence(surviving_nodes, timeout=args.converge_timeout):
        print("Surviving ring did not re-converge to a single cycle; aborting disaster measurement.")
        return
    wait_for_finger_stability(surviving_nodes, timeout=args.converge_timeout)

    print("Executing queries under disaster conditions (nprobe sweep)...")
    disaster_recalls, disaster_perq, disaster_hops, disaster_done = measure(surviving_client, "disaster")
    mean_disaster = disaster_recalls[headline_idx] if disaster_recalls else 0.0
    print(f"Disaster Mean Recall ({len(to_kill)} correlated kills), by nprobe:")
    for npb, br, dr in zip(nprobe_values, baseline_recalls, disaster_recalls):
        print(f"  nprobe={npb:2d} -> baseline {br*100:5.1f}%  disaster {dr*100:5.1f}%  "
              f"(delta {100*(dr-br):+5.1f}pp)")

    # ---- Per-query, per-cluster and region-split reporting (at headline nprobe).
    # This is where the concentrated-vs-diffuse signal lives; the means above
    # hide it. Each query carries the cluster it comes from, so we can group by
    # cluster and split by whether that cluster's primary data was destroyed.
    from collections import defaultdict

    per_query = []
    for qi, gt in enumerate(ground_truth):
        qc = query_clusters[qi]
        b_curve = [baseline_perq[j][qi] for j in range(len(nprobe_values))]
        d_curve = [disaster_perq[j][qi] for j in range(len(nprobe_values))]
        per_query.append({
            "query_cluster": qc,
            "ground_truth_clusters": gt.get("ground_truth_clusters"),
            "in_killed_region": qc in killed_clusters,
            "baseline_by_nprobe": b_curve,
            "disaster_by_nprobe": d_curve,
            "baseline_recall": b_curve[headline_idx],
            "disaster_recall": d_curve[headline_idx],
            "drop": b_curve[headline_idx] - d_curve[headline_idx],
        })

    def _agg(recs):
        n = len(recs)
        if not n:
            return {"n": 0, "mean_baseline": 0.0, "mean_disaster": 0.0, "mean_drop": 0.0}
        mb = sum(r["baseline_recall"] for r in recs) / n
        md = sum(r["disaster_recall"] for r in recs) / n
        return {"n": n, "mean_baseline": mb, "mean_disaster": md, "mean_drop": mb - md}

    # Per-cluster mean recall (grouped by the cluster each query comes from).
    groups = defaultdict(list)
    for rec in per_query:
        groups[rec["query_cluster"]].append(rec)
    per_cluster = {}
    for cid, recs in groups.items():
        a = _agg(recs)
        a["in_killed_region"] = cid in killed_clusters
        per_cluster[str(cid)] = a

    # Region split: queries whose home cluster died vs those untouched.
    in_region = [r for r in per_query if r["in_killed_region"]]
    outside = [r for r in per_query if not r["in_killed_region"]]
    region_summary = {
        "headline_nprobe": head_np,
        "killed_cluster_count": len(killed_clusters),
        "in_killed": _agg(in_region),
        "outside": _agg(outside),
    }

    # Concentration vs diffusion: how many queries dropped, and by how much.
    affected = [r for r in per_query if r["drop"] > 1e-9]
    concentration = {
        "headline_nprobe": head_np,
        "n_queries": len(per_query),
        "n_affected": len(affected),
        "frac_affected": (len(affected) / len(per_query)) if per_query else 0.0,
        "mean_drop_all": (sum(r["drop"] for r in per_query) / len(per_query)) if per_query else 0.0,
        "mean_drop_affected": (sum(r["drop"] for r in affected) / len(affected)) if affected else 0.0,
        "max_drop": max((r["drop"] for r in per_query), default=0.0),
    }

    print(f"\nConcentration (nprobe={head_np}): {concentration['n_affected']}/{concentration['n_queries']} "
          f"queries affected ({concentration['frac_affected']*100:.1f}%); "
          f"mean drop overall {concentration['mean_drop_all']*100:.1f}pp, "
          f"among affected {concentration['mean_drop_affected']*100:.1f}pp, "
          f"max {concentration['max_drop']*100:.1f}pp.")
    print(f"Region split (nprobe={head_np}): "
          f"in-killed n={region_summary['in_killed']['n']} "
          f"{region_summary['in_killed']['mean_baseline']*100:.1f}->{region_summary['in_killed']['mean_disaster']*100:.1f}%  |  "
          f"outside n={region_summary['outside']['n']} "
          f"{region_summary['outside']['mean_baseline']*100:.1f}->{region_summary['outside']['mean_disaster']*100:.1f}%")

    disaster_results = {
        "arch": args.arch,
        "num_nodes": len(node_addresses),
        "nprobe_values": nprobe_values,
        "headline_nprobe": head_np,
        "baseline_recall_by_nprobe": baseline_recalls,
        "disaster_recall_by_nprobe": disaster_recalls,
        "baseline_hops_by_nprobe": baseline_hops,
        "disaster_hops_by_nprobe": disaster_hops,
        "baseline_completed_by_nprobe": baseline_done,
        "disaster_completed_by_nprobe": disaster_done,
        "baseline_recall": mean_baseline,
        "disaster_recall": mean_disaster,
        "correlated_kills": len(to_kill),
        "surviving_nodes": len(surviving_nodes),
        "target_node": target_node,
        "killed_clusters": sorted(killed_clusters),
        "concentration": concentration,
        "region_summary": region_summary,
        "per_cluster": per_cluster,
        "per_query": per_query,
    }

    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized",
                             f"disaster_results_vnode_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(disaster_results, f, indent=4)
    print(f"Vnode disaster results saved to {out_path}")


def bulk_load_direct(addresses, courses, dummy_node, batch_size=400):
    """Offline index construction: place every course DIRECTLY on its canonical
    owner instead of routing sequential PUTs. Three speedups make the full 98k
    corpus loadable in seconds-to-minutes:
      1. cluster assignment is vectorized with numpy (chunked dense TF matrix x
         centroid matrix) instead of one pure-python distance scan per course;
      2. owners are resolved CLIENT-SIDE from the known vnode address list
         (owner of hash h = successor node id >= h, the same successor semantics
         find_successor implements on a converged ring);
      3. courses are shipped per-owner in batched store_bulk RPCs.
    The resulting storage state is identical to sequential insertion for the
    read path under measurement (hops/recall). For large corpora the precomputed
    "vector" cache field is omitted (ranking recomputes it on demand via the
    same vectorizer); for small runs it is embedded exactly as put_course does."""
    import numpy as np

    vocab = dummy_node.vocabulary
    nfeat = dummy_node.n_features
    centroids = np.asarray(dummy_node.centroids, dtype=np.float32)
    cnorm2 = (centroids * centroids).sum(axis=1)
    embed_vectors = len(courses) <= 5000  # keep exact put_course equivalence on small runs

    # 1. Vectorized cluster assignment (chunked to bound memory).
    print(f"Assigning {len(courses)} courses to clusters (numpy, k={len(centroids)})...")
    texts = [f"{c['course_title']} {c['category']} {c['description']}" for c in courses]
    cluster_of = np.empty(len(courses), dtype=np.int32)
    tf_rows = [] if embed_vectors else None
    CH = 2000
    for s in range(0, len(courses), CH):
        chunk = texts[s:s + CH]
        X = np.zeros((len(chunk), nfeat), dtype=np.float32)
        for r, t in enumerate(chunk):
            for tok in t.lower().split():
                j = vocab.get(tok)
                if j is not None:
                    X[r, j] += 1.0
        norms = np.sqrt((X * X).sum(axis=1))
        norms[norms == 0] = 1.0
        X /= norms[:, None]
        cluster_of[s:s + CH] = (cnorm2 - 2.0 * (X @ centroids.T)).argmin(axis=1)
        if embed_vectors:
            tf_rows.extend(X.tolist())

    # 2. Client-side owner resolution: successor of the cluster hash over the
    #    known ring membership (equivalent to find_successor once converged).
    #    get_addr_hash (not get_hash) so this matches how the real nodes hash
    #    their own node_id -- see architectures.async_*.node.get_addr_hash.
    ring = sorted((dummy_node.get_addr_hash(a), a) for a in addresses)
    ring_ids = [h for h, _ in ring]
    import bisect
    def owner_of(h):
        i = bisect.bisect_left(ring_ids, h)
        return ring[i % len(ring)][1]

    per_owner = {}
    for i, c in enumerate(courses):
        cid = int(cluster_of[i])
        owner = owner_of(dummy_node.get_cluster_hash(cid))
        rec = dict(c)
        if embed_vectors:
            rec["vector"] = tf_rows[i]
        per_owner.setdefault(owner, []).append([cid, json.dumps(rec)])

    # 3. Batched delivery.
    placed = 0
    for owner, items in per_owner.items():
        proxy = rpc_client(owner)
        for s in range(0, len(items), batch_size):
            try:
                placed += proxy.store_bulk(items[s:s + batch_size])
            except Exception as e:
                print(f"  store_bulk failed on {owner}: {e}")
    print(f"Bulk-loaded {placed}/{len(courses)} courses onto {len(per_owner)} owner nodes "
          f"({len(set(cluster_of.tolist()))} populated clusters, embed_vectors={embed_vectors}).")


def bulk_load_direct_standard(addresses, courses, dummy_node, batch_size=400):
    """Standard-DHT counterpart of bulk_load_direct: no clustering, each course is
    placed directly by hashing its own course_id (matching put_course's
    get_hash(course_id) scheme) instead of routing sequential PUTs. Owners are
    resolved client-side the same way (successor of the hash over the known ring
    membership), and delivery is batched via store_bulk."""
    embed_vectors = len(courses) <= 5000  # keep exact put_course equivalence on small runs

    # get_addr_hash (not get_hash) so this matches how the real nodes hash
    # their own node_id -- see architectures.async_*.node.get_addr_hash.
    ring = sorted((dummy_node.get_addr_hash(a), a) for a in addresses)
    ring_ids = [h for h, _ in ring]
    import bisect
    def owner_of(h):
        i = bisect.bisect_left(ring_ids, h)
        return ring[i % len(ring)][1]

    per_owner = {}
    for c in courses:
        key_hash = dummy_node.get_hash(c["course_id"])
        owner = owner_of(key_hash)
        rec = dict(c)
        if embed_vectors:
            text = f"{c['course_title']} {c['category']} {c['description']}"
            rec["vector"] = dummy_node._vectorize(text)
        per_owner.setdefault(owner, []).append([str(key_hash), json.dumps(rec)])

    placed = 0
    for owner, items in per_owner.items():
        proxy = rpc_client(owner)
        for s in range(0, len(items), batch_size):
            try:
                placed += proxy.store_bulk(items[s:s + batch_size])
            except Exception as e:
                print(f"  store_bulk failed on {owner}: {e}")
    print(f"Bulk-loaded {placed}/{len(courses)} courses onto {len(per_owner)} owner nodes "
          f"(embed_vectors={embed_vectors}).")


def bulk_inject(args, node_addresses, courses):
    """Fast bulk-load replacement for inject_data_simple: resolves owners
    client-side and ships data via batched store_bulk RPCs instead of routing
    len(courses) sequential put_course calls one at a time. Used as the shared
    data-loading step for every experiment mode (scale/fault/join/disaster/
    characterize) across all six architectures."""
    print(f"Bulk-loading {len(courses)} courses into {args.arch} cluster via {node_addresses[0]}...")
    if base_arch(args.arch) == "standard":
        if args.arch.startswith("async_"):
            from architectures.async_standard_dht.node import NaiveChordNode as HashNode
        else:
            from architectures.standard_dht.node import NaiveChordNode as HashNode
        dummy_node = HashNode("127.0.0.1", 5000)
        bulk_load_direct_standard(node_addresses, courses, dummy_node)
    else:
        HashNode = dummy_node_class(args.arch)
        dummy_node = HashNode("127.0.0.1", 5000)
        bulk_load_direct(node_addresses, courses, dummy_node)


def _forms_single_cycle(addresses, succ):
    """True iff following successor pointers from addresses[0] visits ALL nodes
    exactly once and returns to the start (one valid Chord ring, no gap/partition)."""
    n = len(addresses)
    seen = set()
    cur = addresses[0]
    for _ in range(n):
        if cur not in succ or cur in seen:
            return False
        seen.add(cur)
        cur = succ[cur]
    return cur == addresses[0] and len(seen) == n


def _cycle_reach(addresses, succ):
    """How many distinct nodes the successor chain from addresses[0] reaches
    before looping/gapping -- a progress indicator while the ring forms."""
    seen = set()
    cur = addresses[0]
    for _ in range(len(addresses) + 1):
        if cur not in succ or cur in seen:
            break
        seen.add(cur)
        cur = succ[cur]
    return len(seen)


def wait_for_ring_convergence(addresses, timeout=180, poll_interval=5, stable_polls=2):
    """Poll every node's successor and require they form ONE valid cycle over all
    nodes for `stable_polls` consecutive polls. This certifies lookup CORRECTNESS
    (queries reach the right owner) and rules out a partitioned ring. Finger-table
    (hop-count) settling is handled by a separate wait afterwards. Aborts on timeout
    so we never measure a half-formed ring."""
    n = len(addresses)
    print(f"Waiting for the {n}-node ring to converge to a single valid cycle...")
    deadline = time.time() + timeout
    consecutive = 0
    while time.time() < deadline:
        succ, ok = {}, True
        for a in addresses:
            try:
                succ[a] = rpc_client(a).get_successor()
            except Exception:
                ok = False
                break
        if ok and _forms_single_cycle(addresses, succ):
            consecutive += 1
            if consecutive >= stable_polls:
                print(f"  Ring CONVERGED: single valid cycle over {n} nodes "
                      f"(stable for {stable_polls} polls).")
                return True
        else:
            consecutive = 0
            print(f"  ...forming: successor chain reaches {_cycle_reach(addresses, succ)}/{n} nodes")
        time.sleep(poll_interval)
    print(f"  !! Ring did NOT converge to a single cycle within {timeout}s. "
          f"Aborting to avoid measuring a half-formed ring.")
    return False


def functional_routing_check(client, ring_size, k_clusters=5500, probes=8,
                             attempts=6, wait=20):
    """Can this ring ROUTE? Runs INSIDE the runner (so it streams to `docker logs`)
    and AFTER the successor cycle has closed, which is the only point at which the
    question is meaningful -- asking earlier measures how fast the ring formed.

    Probes cluster ids spread evenly across the id space: they land far apart under
    BOTH placements (SHA-1 scatters them; the semantic linear map puts distant ids
    at distant positions), so they must resolve to several distinct owners via
    multi-hop lookups. Collapsing onto one owner at ~0 hops is the dead-finger
    signature that produced a 2.2%-recall run on 2026-08-21.

    Retries, because fingers populate for a while after the cycle closes."""
    step = max(1, k_clusters // probes)
    cluster_ids = [(i * step) % k_clusters for i in range(probes)]
    min_owners = min(3, max(2, ring_size // 2))

    for attempt in range(1, attempts + 1):
        owners, hop_counts, err = [], [], None
        for cid in cluster_ids:
            try:
                chash = client.get_cluster_hash(cid)
                owner, hops = client.find_successor_with_hops(str(chash))
            except Exception as exc:
                err = f"lookup for cluster {cid} raised {exc}"
                break
            owners.append(owner)
            hop_counts.append(hops)

        if err is None:
            distinct = len(set(owners))
            mean_hops = sum(hop_counts) / len(hop_counts)
            print(f"  routing check [{attempt}/{attempts}] ring={ring_size} "
                  f"distinct owners={distinct}/{len(cluster_ids)} "
                  f"mean hops={mean_hops:.2f} (per-probe {hop_counts})")
            bad = []
            if distinct < min_owners:
                bad.append(f"{len(cluster_ids)} well-separated keys collapsed onto "
                           f"{distinct} owner(s), expected >= {min_owners}")
            if ring_size > 4 and mean_hops < 1.0:
                bad.append(f"mean hops {mean_hops:.2f} < 1.0 -- lookups terminate at "
                           "the successor instead of routing")
            if not bad:
                print("  routing check PASS")
                return True
            for b in bad:
                print(f"    - {b}")
        else:
            print(f"  routing check [{attempt}/{attempts}] {err}")

        if attempt < attempts:
            print(f"    fingers still populating; retrying in {wait}s")
            time.sleep(wait)

    print("  !! ROUTING CHECK FAILED -- refusing to measure on a ring that cannot route.")
    return False


def wait_for_finger_stability(addresses, timeout=600, poll_interval=10, stable_polls=2):
    """Wait until EVERY node reports is_finger_stable() -- i.e. each node's most
    recent complete fix_fingers sweep changed nothing -- for stable_polls
    consecutive polls. All nodes quiescent simultaneously => global finger
    fixpoint => hop counts are converged, not mid-repair. Returns True/False."""
    print(f"Waiting for finger-table quiescence on {len(addresses)} nodes...")
    deadline = time.time() + timeout
    consecutive = 0
    while time.time() < deadline:
        stable = 0
        for a in addresses:
            try:
                if rpc_client(a).is_finger_stable():
                    stable += 1
            except Exception:
                pass
        if stable == len(addresses):
            consecutive += 1
            if consecutive >= stable_polls:
                print(f"  Finger tables QUIESCENT on all {len(addresses)} nodes "
                      f"({stable_polls} consecutive polls).")
                return True
        else:
            consecutive = 0
            print(f"  ...fingers stabilizing: {stable}/{len(addresses)} nodes quiescent")
        time.sleep(poll_interval)
    print(f"  !! Finger tables not fully quiescent within {timeout}s; proceeding, "
          f"but hop counts may include residual repair noise.")
    return False


def run_hops_sweep(args, subset_courses, ground_truth):
    """VIRTUAL-NODE scaling experiment. On a large ring (e.g. 25 vnodes), sweep
    nprobe and measure ROUTING HOPS (the environment-independent metric) for the
    fanout GET, plus recall as a sanity line. Semantic fanout is O(log N + nprobe)
    (adjacent clusters on adjacent nodes); clustered is O(nprobe * log N) (a fresh
    lookup per scattered cluster). Hops are the deliverable."""
    print(f"\n=== Running {args.arch} Virtual-Node Hops Sweep ===")
    addresses = get_vnode_addresses(args)
    entry = addresses[0]
    print(f"Ring: {len(addresses)} virtual nodes across {args.containers} "
          f"({args.vnodes_per_container}/container). Entry: {entry}")
    client = rpc_client(entry)

    # Correctness gate: don't touch the ring until it is a single valid cycle,
    # or bulk-load would resolve owners against a half-formed topology.
    if not wait_for_ring_convergence(addresses, timeout=args.converge_timeout):
        return

    # RF is inherited from the config (RF=2), matching the 5-node scaling run and
    # the thesis, so the two experiments form a controlled RF=2 pair.

    # Direct bulk-load (only needed for the recall sanity line; hops are measured
    # regardless of stored data).
    if base_arch(args.arch) == "standard":
        dummy_node = None
    else:
        HashNode = dummy_node_class(args.arch)
        dummy_node = HashNode("127.0.0.1", 5000)

    if dummy_node is not None:
        bulk_load_direct(addresses, subset_courses, dummy_node)
    else:
        inject_data_simple(entry, subset_courses, args.arch)
    # Cycle correctness established; now wait for the FINGER fixpoint (which sets
    # hop counts). Semantic/clustered expose is_finger_stable(); standard does not,
    # so it falls back to the fixed settle.
    if base_arch(args.arch) in ("semantic", "clustered"):
        wait_for_finger_stability(addresses, timeout=args.converge_timeout)
    else:
        print(f"Letting finger tables settle {args.finger_settle_sec}s...")
        time.sleep(args.finger_settle_sec)

    use_nprobe = base_arch(args.arch) in ("clustered", "semantic")
    k = dummy_node.k if dummy_node is not None else 1
    nprobe_values = [n for n in [1, 2, 3, 5, 8, 12, 20, 40] if n <= max(1, k)] if use_nprobe else [1]

    # Completion bookkeeping: the means below are over queries that RETURNED, so
    # a query that raises (e.g. the clustered fanout timing out at high nprobe,
    # where every scattered cluster costs its own Chord lookup) shrinks the
    # DENOMINATOR instead of moving the mean. Without this, a mean over a
    # surviving subset is indistinguishable from a mean over the full query set,
    # and the reported figure becomes survivorship-biased. attempted/completed
    # are persisted so the denominator is auditable from the artifact alone.
    results = {"arch": args.arch, "num_nodes": len(addresses),
               "nprobe_values": [], "hops": [], "recall": [],
               "attempted": [], "completed": [], "errors": []}
    for npb in nprobe_values:
        hops_list, recalls = [], []
        err_counts = {}
        outcomes = run_query_batch(client, ground_truth, npb,
                                   concurrency=getattr(args, "query_concurrency", 1),
                                   use_nprobe=use_nprobe)
        for gt, outcome in zip(ground_truth, outcomes):
            if isinstance(outcome, Exception):
                name = type(outcome).__name__
                err_counts[name] = err_counts.get(name, 0) + 1
                continue
            (res_tuple, hops), _ = outcome
            retrieved = [json.loads(r)["course_id"] for r in res_tuple]
            hops_list.append(hops)
            recalls.append(compute_recall(retrieved, gt["ground_truth_ids"]))
        mean_h = sum(hops_list) / len(hops_list) if hops_list else 0.0
        mean_r = sum(recalls) / len(recalls) if recalls else 0.0
        results["nprobe_values"].append(npb)
        results["hops"].append(mean_h)
        results["recall"].append(mean_r)
        results["attempted"].append(len(ground_truth))
        results["completed"].append(len(hops_list))
        results["errors"].append(err_counts)
        flag = "" if len(hops_list) == len(ground_truth) else f"  !! PARTIAL {err_counts}"
        print(f"  nprobe={npb:2d} -> hops {mean_h:6.2f} | recall {mean_r*100:5.1f}%"
              f" | n={len(hops_list)}/{len(ground_truth)}{flag}")

    out_path = os.path.join(project_root(), "data", "benchmarks", "results",
                            "containerized", f"hops_sweep_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=4)
    print(f"\nHops sweep results saved to {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", type=str, default="semantic",
                        choices=["standard", "clustered", "semantic",
                                 "async_standard", "async_clustered", "async_semantic"])
    parser.add_argument("--mode", type=str, default="all", choices=["scale", "fault", "join", "disaster", "characterize", "hops", "vnode_disaster", "doomed", "all"])
    parser.add_argument("--dataset", type=str, default="kaggle")
    parser.add_argument("--num_nodes", type=int, default=5)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--dataset_size", type=int, default=500, help="Subset size for tests")
    parser.add_argument("--replication_factor", type=int, default=2, help="DHT replication factor")
    parser.add_argument("--nprobe", type=int, default=2, help="Nprobe for query fanout")
    parser.add_argument("--disaster_kills", type=int, default=3,
                        help="Number of ADJACENT nodes to kill in the disaster scenario "
                             "(models a correlated rack/AZ failure; RF+1 wipes a region entirely -- "
                             "with the default replication_factor=2, that means 3 kills, since "
                             "sync_replicas_to_successors pushes each node's primary data to BOTH "
                             "its 1-hop and 2-hop successor, so 2 kills can never fully wipe a cluster)")
    parser.add_argument("--disaster_cluster", type=int, default=-1,
                        help="vnode_disaster epicenter: target this specific cluster's owner. "
                             "Default -1 = target the heaviest primary node (realistic hot-node "
                             "failure). Set a fixed cluster id for reproducibility across corpora.")
    parser.add_argument("--containers", type=str,
                        default="v-bootstrap,v-node-1,v-node-2,v-node-3,v-node-4",
                        help="Comma-separated container hostnames for the virtual-node ring (mode=hops).")
    parser.add_argument("--vnodes_per_container", type=int, default=5,
                        help="Virtual nodes per container for mode=hops (ring size = containers * this).")
    parser.add_argument("--converge_timeout", type=int, default=180,
                        help="Max seconds to wait for the ring to form a single valid successor cycle (mode=hops).")
    parser.add_argument("--queries_file", type=str, default=None,
                        help="Override the fixed query set (e.g. data/benchmarks/queries/queries_98k_k4096.json "
                             "for the full-corpus scaling experiment).")
    parser.add_argument("--finger_settle_sec", type=int, default=25,
                        help="Seconds to let finger tables settle after the cycle is valid, before measuring hops.")
    parser.add_argument("--gen-queries", action="store_true",
                        help="Pre-generate and save a fixed queries.json for reproducible cross-architecture benchmarks.")
    parser.add_argument("--doomed_queries", type=int, default=15,
                        help="Number of cooked test queries to build from the doomed cluster's own "
                             "member courses (mode=doomed).")
    parser.add_argument("--query_concurrency", type=int, default=1,
                        help="Queries in flight at once during a sweep. 1 = the original "
                             "strictly-sequential loop. Higher values only change wall-clock: "
                             "recall and hops are computed server-side per query. Watch the "
                             "n=<completed>/<total> counter for timeout-induced failures.")
    parser.add_argument("--query_timeout", type=float, default=30.0,
                        help="Per-RPC timeout (s) for the query clients. Raise alongside "
                             "--query_concurrency, since queueing inflates per-query latency.")
    parser.add_argument("--nprobe_list", type=str, default="",
                        help="Comma-separated nprobe values to sweep (mode=doomed). Empty = built-in "
                             "full sweep. Use to trim the low end for faster diagnostic runs, "
                             "e.g. --nprobe_list 1,20,80,320,640")
    args = parser.parse_args()

    # Special mode: just generate the fixed query file and exit
    if args.gen_queries:
        generate_queries(args)
        return

    os.makedirs(os.path.join(project_root(), "data", "benchmarks", "results", "containerized"), exist_ok=True)

    searcher = MonolithicSearcher(dataset=args.dataset, limit=args.dataset_size)
    subset_courses = searcher.courses
    ground_truth = load_ground_truth(args, searcher)

    if args.mode == "scale" or args.mode == "all":
        run_scaling(args, subset_courses, ground_truth)
    if args.mode == "fault" or args.mode == "all":
        run_fault_tolerance(args, subset_courses, ground_truth)
    if args.mode == "join" or args.mode == "all":
        run_node_join(args, subset_courses, ground_truth)
    if args.mode == "disaster" or args.mode == "all":
        run_disaster_scenario(args, subset_courses, ground_truth)
    if args.mode == "characterize" or args.mode == "all":
        run_characterization(args, subset_courses, ground_truth)
    # 'hops', 'vnode_disaster' and 'doomed' target the virtual-node scaling ring
    # (v-* containers), standalone.
    if args.mode == "hops":
        run_hops_sweep(args, subset_courses, ground_truth)
    if args.mode == "vnode_disaster":
        run_vnode_disaster_scenario(args, subset_courses, ground_truth)
    if args.mode == "doomed":
        # Lazy import: doomed_scenario.py imports helpers FROM this module, so
        # importing it at module load time would create a circular import.
        from benchmarks.containerized.doomed_scenario import run_doomed_scenario
        run_doomed_scenario(args, subset_courses)

if __name__ == "__main__":
    main()
