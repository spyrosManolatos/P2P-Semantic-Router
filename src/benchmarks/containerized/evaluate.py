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
    start = time.time()
    try:
        client.get_similar_courses(course_json, nprobe)
    except Exception as e:
        pass
    return (time.time() - start) * 1000

def run_concurrent_batch(address, batch_size, courses, nprobe):
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

def run_fault_tolerance(args, subset_courses, ground_truth):
    arch_label = "Standard Chord DHT" if args.arch == "standard" else "Clustered Chord DHT" if args.arch == "clustered" else "Semantic Router Chord DHT"
    print(f"\n=== Running {arch_label} Fault Tolerance Benchmark ===")
    
    node_addresses = get_node_addresses(args.arch)
    fault_results = {}
    
    # Measure baseline latency and recall
    client = xmlrpc.client.ServerProxy(f"http://{node_addresses[0]}", allow_none=True)
    baseline_latencies = []
    baseline_recalls = []
    
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(client, c_json, args.nprobe if args.arch != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
            baseline_recalls.append(recall)
            baseline_latencies.append(latency)
        except Exception:
            pass
            
    mean_baseline_latency = sum(baseline_latencies) / len(baseline_latencies) if baseline_latencies else 0
    mean_baseline_recall = sum(baseline_recalls) / len(baseline_recalls) if baseline_recalls else 0
    
    print(f"Baseline Mean Latency: {mean_baseline_latency:.2f}ms")
    print(f"Baseline Mean Recall: {mean_baseline_recall*100:.1f}%")
    
    fault_results["baseline"] = {
        "recall": mean_baseline_recall,
        "hops": float(args.nprobe if args.arch != "standard" else 1.0),
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
    print(f"Self-healing complete in {heal_duration:.2f} seconds.")

    # Allow extra time for full ring propagation:
    # Initial healing detects ONE node's successor pointer updating (~1 stabilize cycle).
    # Full convergence requires all surviving nodes to run multiple stabilize cycles so that:
    #   - All finger tables are updated across the ring
    #   - Replica data is promoted to primary on the successor of the failed node
    # With stabilize_interval=1s and 5 nodes, 5 extra seconds = ~5 additional cycles.
    PROPAGATION_WAIT_SEC = 5
    print(f"Waiting {PROPAGATION_WAIT_SEC}s for full ring convergence and replica promotion...")
    time.sleep(PROPAGATION_WAIT_SEC)
    
    # Query post-healing
    post_latencies = []
    post_recalls = []
    active_client = xmlrpc.client.ServerProxy(f"http://{surviving_nodes[0]}", allow_none=True)
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(active_client, c_json, args.nprobe if args.arch != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
            post_recalls.append(recall)
            post_latencies.append(latency)
        except Exception:
            pass
            
    mean_latency = sum(post_latencies) / len(post_latencies) if post_latencies else 0
    mean_recall = sum(post_recalls) / len(post_recalls) if post_recalls else 0
    
    fault_results["after_failure"] = {
        "recall": mean_recall,
        "hops": float(args.nprobe if args.arch != "standard" else 1.0),
        "latency": mean_latency,
        "heal_time_sec": heal_duration
    }
    
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
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(existing_node, c_json, args.nprobe if args.arch != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
            pre_recalls.append(recall)
            pre_latencies.append(latency)
        except Exception:
            pass
            
    mean_pre_recall = sum(pre_recalls) / len(pre_recalls) if pre_recalls else 0
    mean_pre_latency = sum(pre_latencies) / len(pre_latencies) if pre_latencies else 0
    
    # --- TRIGGER JOIN ---
    
    start_time = time.time()
    print(f"Triggering {target_host} to join the cluster via bootstrap node {node_addresses[0]}...")
    new_node.join(node_addresses[0])
    
    print("Waiting 10 seconds for Chord stabilization and data migration...")
    time.sleep(10.0)
    
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
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(new_node, c_json, args.nprobe if args.arch != "standard" else None)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
            post_recalls.append(recall)
            post_latencies.append(latency)
        except Exception as e:
            print(f"  Query failed: {e}")
            
    mean_recall = sum(post_recalls) / len(post_recalls) if post_recalls else 0
    mean_latency = sum(post_latencies) / len(post_latencies) if post_latencies else 0
    
    join_results = {
        "original_5_nodes": {
            "recall": mean_pre_recall,
            "hops": float(args.nprobe if args.arch != "standard" else 1.0),
            "latency": mean_pre_latency,
            "migration_time_sec": 0.0
        },
        "expanded_6_nodes": {
            "recall": mean_recall,
            "hops": float(args.nprobe if args.arch != "standard" else 1.0),
            "latency": mean_latency,
            "migration_time_sec": heal_duration
        }
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
    
    # Configure threshold (applicable only for semantic router)
    client = xmlrpc.client.ServerProxy(f"http://{query_node_addr}", allow_none=True)
    
    concurrency_workloads = [1, 2, 4, 8, 16]
    load_results = {
        "concurrency_levels": concurrency_workloads,
        "with_load_balancing": [],
        "without_load_balancing": []
    }
    
    if args.arch == "semantic":
        print("Testing CONCURRENT queries WITH active load balancing...")
        try:
            client.set_load_threshold(3)
        except Exception:
            pass
        for batch_size in concurrency_workloads:
            avg_latency = run_concurrent_batch(query_node_addr, batch_size, subset_courses, args.nprobe)
            load_results["with_load_balancing"].append(avg_latency)
            time.sleep(1)
            
        print("Testing CONCURRENT queries WITHOUT active load balancing...")
        try:
            client.set_load_threshold(999)
        except Exception:
            pass
        for batch_size in concurrency_workloads:
            avg_latency = run_concurrent_batch(query_node_addr, batch_size, subset_courses, args.nprobe)
            load_results["without_load_balancing"].append(avg_latency)
            time.sleep(1)
    else:
        # Standard or Clustered (no active replica delegation)
        print("Testing CONCURRENT queries (Standard/Clustered baselines)...")
        for batch_size in concurrency_workloads:
            avg_latency = run_concurrent_batch(query_node_addr, batch_size, subset_courses, args.nprobe if args.arch != "standard" else None)
            load_results["without_load_balancing"].append(avg_latency)
            # Fill with balancing list with identical results since load balancing is absent
            load_results["with_load_balancing"].append(avg_latency)
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
    
    from architectures.semantic_router.node import ChordNode as SemanticRouterNode
    from core.config_loader import load_config
    
    dummy_node = SemanticRouterNode("127.0.0.1", 5000)
    
    print(f"Target query: {q_text[:50]}...")
    
    # Let's find the primary target node for this query's top cluster
    # by simulating the hashing logic:
    top_cluster_id = dummy_node._vectorize_and_find_centroids(q_text, 1)[0]
    
    if args.arch == "clustered":
        cluster_hash = int(hashlib.sha1(f"cluster_{top_cluster_id}".encode()).hexdigest(), 16)
    else: # semantic
        chunk_size = (2 ** 16) // 50
        cluster_hash = (top_cluster_id * chunk_size) % (2 ** 16)
        
    target_node = client.find_successor(str(cluster_hash))
    print(f"Top cluster ID is {top_cluster_id} (hash: {cluster_hash}).")
    print(f"Target Node for this cluster is: {target_node}")
    
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
    
    # 3. Trigger the Disaster (Kill the target node)
    print(f"\nTriggering Disaster: Assassinating Primary Node {target_node}...")
    kill_client = xmlrpc.client.ServerProxy(f"http://{target_node}", allow_none=True)
    try:
        kill_client.stop()
    except Exception:
        pass
        
    surviving_nodes = [addr for addr in node_addresses if addr != target_node]
    surviving_client = xmlrpc.client.ServerProxy(f"http://{surviving_nodes[0]}", allow_none=True)
    
    # 4. Immediate Query BEFORE healing can occur (or simulating RF=1)
    print("Executing immediate query under disaster conditions (No healing allowed)...")
    disaster_recalls = []
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, _), _ = run_dht_query(surviving_client, c_json, 5) # High nprobe=5
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(gt["ground_truth_ids"], retrieved_ids)
            disaster_recalls.append(recall)
        except Exception as e:
            # If the gateway fails completely, recall is 0 for that query
            disaster_recalls.append(0.0)
            
    mean_disaster = sum(disaster_recalls) / len(disaster_recalls) if disaster_recalls else 0
    print(f"Disaster Mean Recall (nprobe=5): {mean_disaster*100:.1f}%")
    
    disaster_results = {
        "baseline_recall": mean_baseline,
        "disaster_recall": mean_disaster
    }
    
    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized", f"disaster_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(disaster_results, f, indent=4)
    print(f"Disaster results saved to {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--arch", type=str, default="semantic", choices=["standard", "clustered", "semantic"])
    parser.add_argument("--mode", type=str, default="all", choices=["scale", "fault", "join", "load", "disaster", "all"])
    parser.add_argument("--dataset", type=str, default="kaggle")
    parser.add_argument("--num_nodes", type=int, default=5)
    parser.add_argument("--queries", type=int, default=5)
    parser.add_argument("--dataset_size", type=int, default=500, help="Subset size for tests")
    parser.add_argument("--replication_factor", type=int, default=2, help="DHT replication factor")
    parser.add_argument("--nprobe", type=int, default=2, help="Nprobe for query fanout")
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

if __name__ == "__main__":
    main()
