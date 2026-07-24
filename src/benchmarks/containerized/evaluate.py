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
import json
import random
import argparse
import xmlrpc.client
import socket
import time
import concurrent.futures

socket.setdefaulttimeout(30)

# Add src to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from benchmarks.metrics import compute_recall, timer_decorator
from core.config_loader import load_config
from architectures.monolithic_linear.linear_search import MonolithicSearcher

@timer_decorator
def run_monolithic(searcher, query_text):
    return searcher.search(query_text, top_k=5)

@timer_decorator
def run_dht_query(node_rpc, course_json, nprobe=None):
    if nprobe is None:
        return node_rpc.get_similar_courses(course_json, 1, True)
    else:
        return node_rpc.get_similar_courses(course_json, nprobe, True)

def inject_data_simple(node_address, courses, arch_name):
    print(f"Injecting {len(courses)} courses into {arch_name} cluster via {node_address}...")
    client = xmlrpc.client.ServerProxy(f"http://{node_address}", allow_none=True)
    for i, c in enumerate(courses):
        try:
            client.put_course(json.dumps(c))
        except Exception as e:
            print(f"  Failed to inject course {i}: {e}")
        if (i+1) % 100 == 0:
            print(f"  {i+1}/{len(courses)} injected...")
    print("Injection complete.\n")

# --- CONCURRENCY work for load balancing benchmark ---
def send_concurrency_query(address, course_json, nprobe):
    client = xmlrpc.client.ServerProxy(f"http://{address}", allow_none=True)
    start = time.perf_counter()  # monotonic clock: never runs backwards
    try:
        client.get_similar_courses(course_json, nprobe)
    except Exception as e:
        pass
    return (time.perf_counter() - start) * 1000

def run_concurrent_batch(address, batch_size, courses, nprobe, hotspot_course=None):
    # To exercise active replica delegation we must create a HOTSPOT: every
    # concurrent query targets the SAME course (hence the same cluster, hence
    # the same owner node), concentrating load on a single peer. Sending random
    # courses would spread the load across owners and the mechanism would never
    # trigger. When hotspot_course is None we fall back to a random workload.
    if hotspot_course is not None:
        batch_courses = [hotspot_course] * batch_size
    else:
        batch_courses = [random.choice(courses) for _ in range(batch_size)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as executor:
        futures = []
        for c in batch_courses:
            futures.append(
                executor.submit(send_concurrency_query, address, json.dumps(c), nprobe)
            )
        latencies = [f.result() for f in concurrent.futures.as_completed(futures)]
    return sum(latencies) / len(latencies)

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
    else: # semantic
        return [
            "bootstrap-node:5000",
            "node-1:5000",
            "node-2:5000",
            "node-3:5000",
            "node-4:5000"
        ]

def run_scaling(args, subset_courses, ground_truth):
    arch_label = "Standard Chord DHT" if args.arch == "standard" else "Clustered Chord DHT" if args.arch == "clustered" else "Semantic Router Chord DHT"
    print(f"\n=== Running {arch_label} Scaling & Recall Benchmark ===")
    
    node_addresses = get_node_addresses(args.arch)
    
    # Inject data into cluster
    inject_data_simple(node_addresses[0], subset_courses, args.arch)
    
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
    use_nprobe = (args.arch in ["clustered", "semantic"])
    nprobe_values = [1, 2, 3, 4, 5] if use_nprobe else [1]
    
    for np in nprobe_values:
        np_key = f"nprobe_{np}" if use_nprobe else "standard"
        benchmark_results["dht_results"][np_key] = {
            "recall": [],
            "hops": [],
            "latency": []
        }
        
        print(f"Evaluating {args.arch} with nprobe={np if use_nprobe else 'None'}...")
        # Use a fixed gateway node to eliminate random routing variance
        client = xmlrpc.client.ServerProxy(f"http://{node_addresses[0]}", allow_none=True)
        
        for gt in ground_truth:
            c_json = json.dumps(gt["course"])
            try:
                (res_tuple, hops), latency = run_dht_query(client, c_json, np if use_nprobe else None)
                retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
                
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

    inject_data_simple(node_addresses[0], subset_courses, args.arch)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)

    cluster_totals = {}     # cluster_id -> total primary course count across the ring
    per_node_primary = {}   # node address -> primary course count
    for addr in node_addresses:
        try:
            info = xmlrpc.client.ServerProxy(f"http://{addr}", allow_none=True).get_info()
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
    arch_label = "Standard Chord DHT" if args.arch == "standard" else "Clustered Chord DHT" if args.arch == "clustered" else "Semantic Router Chord DHT"
    print(f"\n=== Running {arch_label} Fault Tolerance Benchmark ===")
    
    node_addresses = get_node_addresses(args.arch)
    fault_results = {}

    # Inject data (each experiment runs on its own fresh ring, so it must
    # populate the ring itself rather than rely on a previous experiment).
    inject_data_simple(node_addresses[0], subset_courses, args.arch)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)

    # Measure baseline latency and recall
    client = xmlrpc.client.ServerProxy(f"http://{node_addresses[0]}", allow_none=True)
    baseline_latencies = []
    baseline_recalls = []
    baseline_hops = []

    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(client, c_json, args.nprobe if args.arch != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
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
    kill_client = xmlrpc.client.ServerProxy(f"http://{failed_node_addr}", allow_none=True)
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
            test_client = xmlrpc.client.ServerProxy(f"http://{surviving_nodes[0]}", allow_none=True)
            info = test_client.get_info()
            if info["successor"] != failed_node_addr:
                healed = True
                break
        except Exception:
            pass
            
    heal_duration = time.time() - start_heal
    print(f"Successor pointer repaired in {heal_duration:.2f} seconds.")

    active_client = xmlrpc.client.ServerProxy(f"http://{surviving_nodes[0]}", allow_none=True)

    def measure_state():
        """Runs the full query set against the surviving ring; returns mean recall/latency/hops."""
        recalls, latencies, hops_list = [], [], []
        for gt in ground_truth:
            c_json = json.dumps(gt["course"])
            try:
                (res_tuple, hops), latency = run_dht_query(active_client, c_json, args.nprobe if args.arch != "standard" else None)
                retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                recalls.append(compute_recall(gt["ground_truth_ids"], retrieved_ids))
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
    fault_results["nprobe"] = args.nprobe if args.arch != "standard" else 1

    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized", f"fault_tolerance_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(fault_results, f, indent=4)
    print(f"Fault tolerance results saved to {out_path}")

def run_node_join(args, subset_courses, ground_truth):
    arch_label = "Standard Chord DHT" if args.arch == "standard" else "Clustered Chord DHT" if args.arch == "clustered" else "Semantic Router Chord DHT"
    print(f"\n=== Running {arch_label} Node Join Benchmark ===")
    
    node_addresses = get_node_addresses(args.arch)
    
    # 1. Inject data
    inject_data_simple(node_addresses[0], subset_courses, args.arch)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)
    
    # Target the dedicated 6th node container running idle in the docker network
    target_host = "standard-node-5" if args.arch == "standard" else "clustered-node-5" if args.arch == "clustered" else "node-5"
    target_addr = f"{target_host}:5000"
    
    print(f"Connecting to idle container {target_host}...")
    new_node = xmlrpc.client.ServerProxy(f"http://{target_addr}", allow_none=True)
    existing_node = xmlrpc.client.ServerProxy(f"http://{node_addresses[0]}", allow_none=True)
    
    # --- QUERY BEFORE JOIN ---
    print("Executing queries on the original 5-node ring...")
    pre_latencies = []
    pre_recalls = []
    pre_hops = []
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(existing_node, c_json, args.nprobe if args.arch != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
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
            (res_tuple, hops), latency = run_dht_query(new_node, c_json, args.nprobe if args.arch != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
            post_recalls.append(recall)
            post_latencies.append(latency)
            post_hops.append(hops)
        except Exception as e:
            print(f"  Query failed: {e}")

    mean_recall = sum(post_recalls) / len(post_recalls) if post_recalls else 0
    mean_latency = sum(post_latencies) / len(post_latencies) if post_latencies else 0
    mean_post_hops = sum(post_hops) / len(post_hops) if post_hops else 0

    join_results = {
        "nprobe": args.nprobe if args.arch != "standard" else 1,
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

def run_load_balancing(args, subset_courses, ground_truth):
    arch_label = "Standard Chord DHT" if args.arch == "standard" else "Clustered Chord DHT" if args.arch == "clustered" else "Semantic Router Chord DHT"
    print(f"\n=== Running {arch_label} Concurrency Benchmark ===")
    
    node_addresses = get_node_addresses(args.arch)
    query_node_addr = node_addresses[0]

    client = xmlrpc.client.ServerProxy(f"http://{query_node_addr}", allow_none=True)

    concurrency_workloads = [1, 2, 4, 8, 16]
    load_results = {
        "concurrency_levels": concurrency_workloads,
        "with_load_balancing": [],
        "without_load_balancing": []
    }

    # Active replica delegation is a Semantic Router feature; the baselines have
    # no equivalent, so this experiment is meaningful only for the semantic arch.
    if args.arch != "semantic":
        print(f"Skipping load-balancing benchmark for '{args.arch}': "
              f"no active replica delegation in this architecture.")
        return

    # Inject data (fresh ring per experiment): without data the hotspot node has
    # nothing to serve and we would only measure empty-query overhead.
    inject_data_simple(node_addresses[0], subset_courses, args.arch)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)

    # A single fixed course is queried repeatedly to concentrate all load on one
    # owner node (a hotspot), which is what triggers replica delegation.
    hotspot = random.choice(subset_courses)
    print(f"Hotspot course: '{hotspot.get('course_title', '?')[:60]}'")

    print("Testing CONCURRENT queries WITH active load balancing (threshold=3)...")
    try:
        client.set_load_threshold(3)
    except Exception:
        pass
    for batch_size in concurrency_workloads:
        avg_latency = run_concurrent_batch(query_node_addr, batch_size, subset_courses,
                                           args.nprobe, hotspot_course=hotspot)
        load_results["with_load_balancing"].append(avg_latency)
        time.sleep(1)

    print("Testing CONCURRENT queries WITHOUT active load balancing (threshold=999)...")
    try:
        client.set_load_threshold(999)  # effectively never delegates
    except Exception:
        pass
    for batch_size in concurrency_workloads:
        avg_latency = run_concurrent_batch(query_node_addr, batch_size, subset_courses,
                                           args.nprobe, hotspot_course=hotspot)
        load_results["without_load_balancing"].append(avg_latency)
        time.sleep(1)
            
    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized", f"load_balancing_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(load_results, f, indent=4)
    print(f"Load balancing results saved to {out_path}")

def project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

def queries_file_path():
    return os.path.join(project_root(), "data", "benchmarks", "queries.json")


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


def run_disaster_scenario(args, subset_courses, ground_truth):
    if args.arch == "standard":
        print("Skipping disaster scenario for Standard DHT (nprobe logic does not apply).")
        return
        
    print(f"\n=== Running {args.arch} Disaster Scenario ===")
    
    node_addresses = get_node_addresses(args.arch)
    client = xmlrpc.client.ServerProxy(f"http://{node_addresses[0]}", allow_none=True)
    
    # 1. Inject data
    inject_data_simple(node_addresses[0], subset_courses, args.arch)
    print("Waiting 15s for full ring stabilization and finger table propagation...")
    time.sleep(15)
    
    # 2. Pick a single target query and find its primary cluster
    test_query = ground_truth[0]
    q_text = test_query["query"]
    
    if args.arch == "clustered":
        from architectures.clustered_dht.node import ChordNode as HashNode
    else:
        from architectures.semantic_router.node import ChordNode as HashNode

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
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
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
    walker = xmlrpc.client.ServerProxy(f"http://{target_node}", allow_none=True)
    for _ in range(kills - 1):
        try:
            succ = walker.get_successor()
        except Exception:
            break
        if not succ or succ in to_kill:
            break
        to_kill.append(succ)
        walker = xmlrpc.client.ServerProxy(f"http://{succ}", allow_none=True)

    print(f"\nTriggering CORRELATED Disaster: killing {len(to_kill)} adjacent nodes: {to_kill}")
    for addr in to_kill:
        try:
            xmlrpc.client.ServerProxy(f"http://{addr}", allow_none=True).stop()
        except Exception:
            pass

    surviving_nodes = [addr for addr in node_addresses if addr not in to_kill]
    if not surviving_nodes:
        print("All nodes killed; aborting disaster measurement.")
        return
    surviving_client = xmlrpc.client.ServerProxy(f"http://{surviving_nodes[0]}", allow_none=True)

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
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
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
    ring = sorted((dummy_node.get_hash(a), a) for a in addresses)
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
        proxy = xmlrpc.client.ServerProxy(f"http://{owner}", allow_none=True)
        for s in range(0, len(items), batch_size):
            try:
                placed += proxy.store_bulk(items[s:s + batch_size])
            except Exception as e:
                print(f"  store_bulk failed on {owner}: {e}")
    print(f"Bulk-loaded {placed}/{len(courses)} courses onto {len(per_owner)} owner nodes "
          f"({len(set(cluster_of.tolist()))} populated clusters, embed_vectors={embed_vectors}).")


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
                succ[a] = xmlrpc.client.ServerProxy(f"http://{a}", allow_none=True).get_successor()
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
                if xmlrpc.client.ServerProxy(f"http://{a}", allow_none=True).is_finger_stable():
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
    client = xmlrpc.client.ServerProxy(f"http://{entry}", allow_none=True)

    # Correctness gate: don't touch the ring until it is a single valid cycle,
    # or bulk-load would resolve owners against a half-formed topology.
    if not wait_for_ring_convergence(addresses, timeout=args.converge_timeout):
        return

    # RF=0: pure GET-routing experiment, no replica placement needed.
    # ALSO disable load-balancing delegation: with RF=0 there is no replica, so an
    # "overloaded" node (load_threshold=3) would delegate reads to an EMPTY successor
    # and return nothing -- which corrupts recall (worse at higher nprobe). Raise the
    # threshold so every node always serves its own data.
    for a in addresses:
        try:
            proxy = xmlrpc.client.ServerProxy(f"http://{a}", allow_none=True)
            proxy.set_replication_factor(0)
            proxy.set_load_threshold(10 ** 9)
        except Exception:
            pass

    # Direct bulk-load (only needed for the recall sanity line; hops are measured
    # regardless of stored data).
    if args.arch == "standard":
        from architectures.standard_dht.node import NaiveChordNode as HashNode
        dummy_node = None
    elif args.arch == "clustered":
        from architectures.clustered_dht.node import ChordNode as HashNode
        dummy_node = HashNode("127.0.0.1", 5000)
    else:
        from architectures.semantic_router.node import ChordNode as HashNode
        dummy_node = HashNode("127.0.0.1", 5000)

    if dummy_node is not None:
        bulk_load_direct(addresses, subset_courses, dummy_node)
    else:
        inject_data_simple(entry, subset_courses, args.arch)
    # Cycle correctness established; now wait for the FINGER fixpoint (which sets
    # hop counts). Semantic/clustered expose is_finger_stable(); standard does not,
    # so it falls back to the fixed settle.
    if args.arch in ("semantic", "clustered"):
        wait_for_finger_stability(addresses, timeout=args.converge_timeout)
    else:
        print(f"Letting finger tables settle {args.finger_settle_sec}s...")
        time.sleep(args.finger_settle_sec)

    use_nprobe = args.arch in ("clustered", "semantic")
    k = dummy_node.k if dummy_node is not None else 1
    nprobe_values = [n for n in [1, 2, 3, 5, 8, 12, 20, 40] if n <= max(1, k)] if use_nprobe else [1]

    results = {"arch": args.arch, "num_nodes": len(addresses),
               "nprobe_values": [], "hops": [], "recall": []}
    for npb in nprobe_values:
        hops_list, recalls = [], []
        for gt in ground_truth:
            c_json = json.dumps(gt["course"])
            try:
                (res_tuple, hops), _ = run_dht_query(client, c_json, npb if use_nprobe else None)
                retrieved = [json.loads(r)["course_id"] for r in res_tuple]
                hops_list.append(hops)
                recalls.append(compute_recall(gt["ground_truth_ids"], retrieved))
            except Exception:
                pass
        mean_h = sum(hops_list) / len(hops_list) if hops_list else 0.0
        mean_r = sum(recalls) / len(recalls) if recalls else 0.0
        results["nprobe_values"].append(npb)
        results["hops"].append(mean_h)
        results["recall"].append(mean_r)
        print(f"  nprobe={npb:2d} -> hops {mean_h:6.2f} | recall {mean_r*100:5.1f}%")

    out_path = os.path.join(project_root(), "data", "benchmarks", "results",
                            "containerized", f"hops_sweep_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=4)
    print(f"\nHops sweep results saved to {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", type=str, default="semantic", choices=["standard", "clustered", "semantic"])
    parser.add_argument("--mode", type=str, default="all", choices=["scale", "fault", "join", "load", "disaster", "characterize", "hops", "all"])
    parser.add_argument("--dataset", type=str, default="kaggle")
    parser.add_argument("--num_nodes", type=int, default=5)
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--dataset_size", type=int, default=500, help="Subset size for tests")
    parser.add_argument("--replication_factor", type=int, default=2, help="DHT replication factor")
    parser.add_argument("--nprobe", type=int, default=2, help="Nprobe for query fanout")
    parser.add_argument("--disaster_kills", type=int, default=2,
                        help="Number of ADJACENT nodes to kill in the disaster scenario "
                             "(models a correlated rack/AZ failure; RF+1 wipes a region entirely)")
    parser.add_argument("--containers", type=str,
                        default="v-bootstrap,v-node-1,v-node-2,v-node-3,v-node-4",
                        help="Comma-separated container hostnames for the virtual-node ring (mode=hops).")
    parser.add_argument("--vnodes_per_container", type=int, default=5,
                        help="Virtual nodes per container for mode=hops (ring size = containers * this).")
    parser.add_argument("--converge_timeout", type=int, default=180,
                        help="Max seconds to wait for the ring to form a single valid successor cycle (mode=hops).")
    parser.add_argument("--queries_file", type=str, default=None,
                        help="Override the fixed query set (e.g. data/benchmarks/queries_98k_k4096.json "
                             "for the full-corpus scaling experiment).")
    parser.add_argument("--finger_settle_sec", type=int, default=25,
                        help="Seconds to let finger tables settle after the cycle is valid, before measuring hops.")
    parser.add_argument("--gen-queries", action="store_true",
                        help="Pre-generate and save a fixed queries.json for reproducible cross-architecture benchmarks.")
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
    if args.mode == "load" or args.mode == "all":
        run_load_balancing(args, subset_courses, ground_truth)
    if args.mode == "disaster" or args.mode == "all":
        run_disaster_scenario(args, subset_courses, ground_truth)
    if args.mode == "characterize" or args.mode == "all":
        run_characterization(args, subset_courses, ground_truth)
    # 'hops' targets the virtual-node scaling ring (v-* containers), standalone.
    if args.mode == "hops":
        run_hops_sweep(args, subset_courses, ground_truth)

if __name__ == "__main__":
    main()
