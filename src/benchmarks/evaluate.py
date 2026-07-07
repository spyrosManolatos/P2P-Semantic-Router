import os
import sys
import json
import random
import argparse
import xmlrpc.client
import socket
import time
import concurrent.futures

socket.setdefaulttimeout(30)

# Add src to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.metrics import compute_recall, timer_decorator
from core.config_loader import load_config
from architectures.monolithic_linear.linear_search import MonolithicSearcher

# Import setup utilities
from architectures.standard_dht.simulations.utils import setup_network as setup_standard, teardown_network as teardown_standard
from architectures.clustered_dht.simulations.utils import setup_network as setup_clustered, teardown_network as teardown_clustered
from architectures.semantic_router.simulations.utils import setup_network as setup_semantic, teardown_network as teardown_semantic
from architectures.semantic_router.node import ChordNode

@timer_decorator
def run_monolithic(searcher, query_text):
    return searcher.search(query_text, top_k=5)

@timer_decorator
def run_dht_query(node_rpc, course_json, nprobe=None):
    if nprobe is None:
        return node_rpc.get_similar_courses(course_json, 1, True)
    else:
        return node_rpc.get_similar_courses(course_json, nprobe, True)

def inject_data_simple(nodes, courses, arch_name):
    print(f"Injecting {len(courses)} courses into {arch_name}...")
    client = xmlrpc.client.ServerProxy(f"http://{nodes[0].address}")
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
    client = xmlrpc.client.ServerProxy(f"http://{address}")
    start = time.time()
    try:
        client.get_similar_courses(course_json, nprobe)
    except Exception as e:
        pass
    return (time.time() - start) * 1000

def run_concurrent_batch(address, batch_size, courses, nprobe):
    batch_courses = [random.choice(courses) for _ in range(batch_size)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=batch_size) as executor:
        futures = [
            executor.submit(send_concurrency_query, address, json.dumps(c), nprobe)
            for c in batch_courses
        ]
        latencies = [f.result() for f in concurrent.futures.as_completed(futures)]
    return sum(latencies) / len(latencies)

def set_network_threshold(nodes, threshold):
    for node in nodes:
        try:
            client = xmlrpc.client.ServerProxy(f"http://{node.address}")
            client.set_load_threshold(threshold)
        except Exception:
            pass

# =====================================================================
# BENCHMARK MODES
# =====================================================================

def run_scaling(args, subset_courses, ground_truth):
    print("=== Running Multi-Architecture Scaling Benchmark ===")
    nprobes_to_test = [1, 2, 3, 4, 5]
    benchmark_results = {
        "standard_dht": {"recall": [], "hops": [], "latency": []},
        "clustered_dht": {f"nprobe_{n}": {"recall": [], "hops": [], "latency": []} for n in nprobes_to_test},
        "semantic_router": {f"nprobe_{n}": {"recall": [], "hops": [], "latency": []} for n in nprobes_to_test}
    }

    def evaluate_arch(arch_name, setup_fn, teardown_fn, base_port, is_semantic=False):
        print(f"\n--- Evaluating {arch_name} ---")
        nodes = setup_fn(num_nodes=args.num_nodes, base_port=base_port, dataset=args.dataset)
        print("Waiting 10s for Chord ring stabilization...")
        time.sleep(10)
        inject_data_simple(nodes, subset_courses, arch_name)
        
        client = xmlrpc.client.ServerProxy(f"http://{nodes[0].address}")
        use_nprobe = (is_semantic or arch_name == "clustered_dht")
        
        for gt in ground_truth:
            c_json = json.dumps(gt["course"])
            nprobes = nprobes_to_test if use_nprobe else [None]
            for np in nprobes:
                try:
                    (res_tuple, hops), latency = run_dht_query(client, c_json, np)
                    retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                    recall = compute_recall(retrieved_ids, gt["gt_ids"])
                except Exception:
                    recall, hops, latency = 0.0, 0.0, 0.0
                    
                key = f"nprobe_{np}" if use_nprobe else arch_name
                target_dict = benchmark_results[arch_name][key] if use_nprobe else benchmark_results[key]
                target_dict["recall"].append(recall)
                target_dict["hops"].append(hops)
                target_dict["latency"].append(latency)
                
        teardown_fn(nodes)
        print(f"{arch_name} evaluation complete.")

    evaluate_arch("standard_dht", setup_standard, teardown_standard, 8100)
    evaluate_arch("clustered_dht", setup_clustered, teardown_clustered, 8200)
    evaluate_arch("semantic_router", setup_semantic, teardown_semantic, 8300, is_semantic=True)

    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "results.json")
    with open(out_path, "w") as f:
        json.dump(benchmark_results, f, indent=4)
    print(f"Scaling results saved to {out_path}")

def run_fault_tolerance(args, subset_courses, ground_truth):
    print("\n=== Running Fault Tolerance & Self-Healing Benchmark ===")
    nodes = setup_semantic(num_nodes=args.num_nodes, base_port=8400, r=args.replication_factor, dataset=args.dataset)
    print("Waiting 10s for Chord ring stabilization...")
    time.sleep(10)
    
    inject_data_simple(nodes, subset_courses, "semantic_router")
    print("Waiting 5s for replicas to sync...")
    time.sleep(5)
    
    active_clusters = set()
    for n in nodes:
        try:
            proxy = xmlrpc.client.ServerProxy(f"http://{n.address}")
            info = proxy.get_info()
            active_clusters.update(info["primary_summary"].keys())
        except Exception:
            pass
            
    eval_states = [
        {"name": "0% Failure (Healthy)", "nodes_to_kill": 0},
        {"name": "16.7% Failure (1 Node Killed)", "nodes_to_kill": 1},
        {"name": "33.3% Failure (2 Nodes Killed)", "nodes_to_kill": 1}
    ]
    
    fault_results = {}
    dead_addresses = set()
    
    for state in eval_states:
        kill_count = state["nodes_to_kill"]
        state_name = state["name"]
        heal_duration = 0.0
        
        if kill_count > 0:
            print(f"\nKilling {kill_count} random node(s)...")
            for _ in range(kill_count):
                if len(nodes) <= 1:
                    break
                failed_node = random.choice(nodes)
                print(f"Killing node: {failed_node.address}...")
                dead_addresses.add(failed_node.address)
                failed_node.stop()
                nodes.remove(failed_node)
            
            print("Monitoring network healing progress...")
            heal_start = time.time()
            healed = False
            
            for attempt in range(300):
                time.sleep(0.1)
                all_ok = True
                alive_primaries = set()
                
                for n in nodes:
                    try:
                        proxy = xmlrpc.client.ServerProxy(f"http://{n.address}")
                        info = proxy.get_info()
                        if info["successor"] in dead_addresses or info["predecessor"] in dead_addresses:
                            all_ok = False
                            break
                        if not info["successor"] or not info["predecessor"]:
                            all_ok = False
                            break
                        alive_primaries.update(info["primary_summary"].keys())
                    except Exception:
                        all_ok = False
                        break
                
                if all_ok and set(alive_primaries) == active_clusters:
                    healed = True
                    heal_duration = time.time() - heal_start
                    break
                    
            if healed:
                print(f"🟢 Ring Fully Healed in {heal_duration:.2f} seconds!")
            else:
                print("🔴 Ring failed to fully stabilize within 30 seconds.")
                time.sleep(5)
                
        print(f"--- Testing state: {state_name} ---")
        state_recalls, state_hops, state_latencies = [], [], []
        query_node = nodes[0]
        client = xmlrpc.client.ServerProxy(f"http://{query_node.address}")
        
        for gt in ground_truth:
            c_json = json.dumps(gt["course"])
            try:
                (res_tuple, hops), latency = run_dht_query(client, c_json, args.nprobe)
                retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                recall = compute_recall(retrieved_ids, gt["gt_ids"])
                state_recalls.append(recall)
                state_hops.append(hops)
                state_latencies.append(latency)
            except Exception:
                state_recalls.append(0.0)
                state_hops.append(0.0)
                state_latencies.append(0.0)
                
        mean_recall = sum(state_recalls) / len(state_recalls) if state_recalls else 0.0
        mean_hops = sum(state_hops) / len(state_hops) if state_hops else 0.0
        mean_latency = sum(state_latencies) / len(state_latencies) if state_latencies else 0.0
        
        print(f"Results: Recall={mean_recall*100:.1f}% | Hops={mean_hops:.1f} | Latency={mean_latency:.1f}ms")
        fault_results[state_name] = {
            "recall": mean_recall,
            "hops": mean_hops,
            "latency": mean_latency,
            "heal_time_sec": heal_duration
        }
        
    teardown_semantic(nodes)
    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "fault_tolerance_results.json")
    with open(out_path, "w") as f:
        json.dump(fault_results, f, indent=4)
    print(f"Fault tolerance results saved to {out_path}")

def run_node_join(args, subset_courses, ground_truth):
    print("\n=== Running Dynamic Node Join Benchmark ===")
    nodes = setup_semantic(num_nodes=args.num_nodes, base_port=8500, r=args.replication_factor, dataset=args.dataset)
    print("Waiting 10s for Chord ring stabilization...")
    time.sleep(10)
    
    inject_data_simple(nodes, subset_courses, "semantic_router")
    print("Waiting 5s for replication to settle...")
    time.sleep(5)
    
    active_clusters = set()
    for n in nodes:
        try:
            proxy = xmlrpc.client.ServerProxy(f"http://{n.address}")
            info = proxy.get_info()
            active_clusters.update(info["primary_summary"].keys())
        except Exception:
            pass
            
    print(f"--- Testing state: {args.num_nodes} Nodes (Baseline) ---")
    baseline_recalls, baseline_hops, baseline_latencies = [], [], []
    client = xmlrpc.client.ServerProxy(f"http://{nodes[0].address}")
    
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(client, c_json, args.nprobe)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(retrieved_ids, gt["gt_ids"])
            baseline_recalls.append(recall)
            baseline_hops.append(hops)
            baseline_latencies.append(latency)
        except Exception:
            pass
            
    mean_baseline_recall = sum(baseline_recalls) / len(baseline_recalls) if baseline_recalls else 0.0
    mean_baseline_hops = sum(baseline_hops) / len(baseline_hops) if baseline_hops else 0.0
    mean_baseline_latency = sum(baseline_latencies) / len(baseline_latencies) if baseline_latencies else 0.0
    print(f"Results: Recall={mean_baseline_recall*100:.1f}% | Hops={mean_baseline_hops:.1f} | Latency={mean_baseline_latency:.1f}ms")

    # Join 6th node
    print("\nJoining the 6th Node...")
    new_port = 8500 + args.num_nodes
    new_node = ChordNode("127.0.0.1", new_port, dataset=args.dataset)
    new_node.start()
    
    join_start = time.time()
    new_node.join(nodes[0].address)
    nodes.append(new_node)
    
    print("Monitoring data migration and ring healing progress...")
    healed = False
    heal_duration = 0.0
    for attempt in range(300):
        time.sleep(0.1)
        all_ok = True
        alive_primaries = set()
        
        for n in nodes:
            try:
                proxy = xmlrpc.client.ServerProxy(f"http://{n.address}")
                info = proxy.get_info()
                if not info["successor"] or not info["predecessor"]:
                    all_ok = False
                    break
                alive_primaries.update(info["primary_summary"].keys())
            except Exception:
                all_ok = False
                break
                
        if all_ok and set(alive_primaries) == active_clusters:
            healed = True
            heal_duration = time.time() - join_start
            break
            
    if healed:
        print(f"🟢 Ring stabilized and data migrated in {heal_duration:.2f} seconds!")
    else:
        print("🔴 Ring failed to stabilize within 30 seconds.")
        time.sleep(5)
        
    print(f"--- Testing state: {args.num_nodes + 1} Nodes (Expanded Network) ---")
    expanded_recalls, expanded_hops, expanded_latencies = [], [], []
    client = xmlrpc.client.ServerProxy(f"http://{new_node.address}")
    
    for gt in ground_truth:
        c_json = json.dumps(gt["course"])
        try:
            (res_tuple, hops), latency = run_dht_query(client, c_json, args.nprobe)
            retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
            recall = compute_recall(retrieved_ids, gt["gt_ids"])
            expanded_recalls.append(recall)
            expanded_hops.append(hops)
            expanded_latencies.append(latency)
        except Exception:
            pass
            
    mean_expanded_recall = sum(expanded_recalls) / len(expanded_recalls) if expanded_recalls else 0.0
    mean_expanded_hops = sum(expanded_hops) / len(expanded_hops) if expanded_hops else 0.0
    mean_expanded_latency = sum(expanded_latencies) / len(expanded_latencies) if expanded_latencies else 0.0
    print(f"Results: Recall={mean_expanded_recall*100:.1f}% | Hops={mean_expanded_hops:.1f} | Latency={mean_expanded_latency:.1f}ms")

    teardown_semantic(nodes)
    join_results = {
        "baseline_5_nodes": {
            "recall": mean_baseline_recall,
            "hops": mean_baseline_hops,
            "latency": mean_baseline_latency,
            "migration_time_sec": 0.0
        },
        "expanded_6_nodes": {
            "recall": mean_expanded_recall,
            "hops": mean_expanded_hops,
            "latency": mean_expanded_latency,
            "migration_time_sec": heal_duration
        }
    }
    
    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "node_join_results.json")
    with open(out_path, "w") as f:
        json.dump(join_results, f, indent=4)
    print(f"Node join results saved to {out_path}")

def run_load_balancing(args, subset_courses, ground_truth):
    print("\n=== Running Concurrency & Active Load Balancing Benchmark ===")
    nodes = setup_semantic(num_nodes=args.num_nodes, base_port=8600, r=args.replication_factor, dataset=args.dataset)
    print("Waiting 10s for Chord ring stabilization...")
    time.sleep(10)
    
    inject_data_simple(nodes, subset_courses, "semantic_router")
    print("Waiting 5s for replication to settle...")
    time.sleep(5)
    
    concurrency_workloads = [1, 2, 4, 8, 12, 16]
    load_results = {
        "concurrency_levels": concurrency_workloads,
        "with_load_balancing": [],
        "without_load_balancing": []
    }
    
    # Test 1: With LB (threshold=3)
    print("Evaluating WITH Active Load Balancing (Threshold = 3)...")
    set_network_threshold(nodes, 3)
    time.sleep(1)
    
    query_node_addr = nodes[0].address
    for batch_size in concurrency_workloads:
        avg_latency = run_concurrent_batch(query_node_addr, batch_size, subset_courses, args.nprobe)
        load_results["with_load_balancing"].append(avg_latency)
        time.sleep(1)
        
    # Test 2: Without LB (threshold=99999)
    print("Evaluating WITHOUT Load Balancing (Delegation Disabled)...")
    set_network_threshold(nodes, 99999)
    time.sleep(1)
    
    for batch_size in concurrency_workloads:
        avg_latency = run_concurrent_batch(query_node_addr, batch_size, subset_courses, args.nprobe)
        load_results["without_load_balancing"].append(avg_latency)
        time.sleep(1)
        
    teardown_semantic(nodes)
    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "load_balancing_results.json")
    with open(out_path, "w") as f:
        json.dump(load_results, f, indent=4)
    print(f"Load balancing results saved to {out_path}")


# =====================================================================
# SYSTEM UTILS
# =====================================================================
def project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", type=str, default="all", choices=["scale", "fault", "join", "load", "all"])
    parser.add_argument("--dataset", type=str, default="kaggle")
    parser.add_argument("--num_nodes", type=int, default=6)
    parser.add_argument("--queries", type=int, default=5)
    parser.add_argument("--dataset_size", type=int, default=500, help="Subset size for tests")
    parser.add_argument("--replication_factor", type=int, default=3, help="DHT replication factor")
    parser.add_argument("--nprobe", type=int, default=2, help="Nprobe for query fanout")
    args = parser.parse_args()

    os.makedirs(os.path.join(project_root(), "data", "benchmarks", "results"), exist_ok=True)

    # Precompute monolithic ground truth
    searcher = MonolithicSearcher(dataset=args.dataset)
    # Match database subset size
    subset_courses = searcher.courses[:args.dataset_size]
    searcher.courses = subset_courses
    
    test_courses = random.sample(subset_courses, min(args.queries, len(subset_courses)))
    ground_truth = []
    
    print("Pre-computing Monolithic Ground Truth baselines...")
    for c in test_courses:
        q_text = f"{c['course_title']} {c['category']} {c['description']}"
        results, latency = run_monolithic(searcher, q_text)
        gt_ids = [res['course_id'] for sim, res in results]
        ground_truth.append({"course": c, "gt_ids": gt_ids, "mono_latency": latency})

    if args.mode in ["scale", "all"]:
        # Scale runs with 2000 size if all or scale selected (to match scaling parameters)
        scale_args = argparse.Namespace(**vars(args))
        if scale_args.dataset_size == 500: # If default, bump it to 2000 for scaling comparisons
            scale_args.dataset_size = 2000
        # Re-fetch courses for 2000 subset
        scale_courses = MonolithicSearcher(dataset=args.dataset).courses[:scale_args.dataset_size]
        run_scaling(scale_args, scale_courses, ground_truth)
        
    if args.mode in ["fault", "all"]:
        run_fault_tolerance(args, subset_courses, ground_truth)
        
    if args.mode in ["join", "all"]:
        # Node join baseline starts at 5 nodes
        join_args = argparse.Namespace(**vars(args))
        join_args.num_nodes = 5
        run_node_join(join_args, subset_courses, ground_truth)
        
    if args.mode in ["load", "all"]:
        run_load_balancing(args, subset_courses, ground_truth)

if __name__ == "__main__":
    main()
