import os
import sys
import json
import random
import argparse
import xmlrpc.client
import socket
import time

socket.setdefaulttimeout(120)

# Add src to Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from benchmarks.metrics import compute_recall, timer_decorator
from core.config_loader import load_config
from architectures.monolithic_linear.linear_search import MonolithicSearcher

# Import setup utilities
from architectures.standard_dht.simulations.utils import setup_network as setup_standard, teardown_network as teardown_standard
from architectures.clustered_dht.simulations.utils import setup_network as setup_clustered, teardown_network as teardown_clustered
from architectures.semantic_router.simulations.utils import setup_network as setup_semantic, teardown_network as teardown_semantic

@timer_decorator
def run_monolithic(searcher, query_text):
    return searcher.search(query_text, top_k=5)

@timer_decorator
def run_dht_query(node_rpc, course_json, nprobe=None):
    if nprobe is None:
        # Standard / Clustered
        return node_rpc.get_similar_courses(course_json, 1, True)
    else:
        # Semantic
        return node_rpc.get_similar_courses(course_json, nprobe, True)

def inject_data(nodes, courses, arch_name):
    print(f"Injecting {len(courses)} courses into {arch_name}...")
    # Inject via RPC to the first node to simulate real client behavior
    client = xmlrpc.client.ServerProxy(f"http://{nodes[0].address}")
    for i, c in enumerate(courses):
        client.put_course(json.dumps(c))
        if (i+1) % 50 == 0:
            print(f"  {i+1}/{len(courses)} injected...")
    print("Injection complete.\n")

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=str, default="kaggle")
    parser.add_argument("--num_nodes", type=int, default=5)
    parser.add_argument("--queries", type=int, default=10)
    parser.add_argument("--dataset_size", type=int, default=2000, help="Subset size to inject to keep tests fast")
    parser.add_argument("--num_clusters", type=int, default=None, help="Retrain centroids with this k value before benchmarking")
    args = parser.parse_args()
    
    if args.num_clusters is not None:
        import subprocess
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        train_script = os.path.join(project_root, "src", "ml", "train_centroids.py")
        print(f"Re-training K-Means centroids with k={args.num_clusters} clusters...")
        subprocess.run([sys.executable, train_script, "--k", str(args.num_clusters), "--dataset", args.dataset], check=True)
        print("Centroids re-trained successfully.\n")

    print(f"=== Starting Benchmarks ===")
    print(f"Dataset: {args.dataset} | Subset: {args.dataset_size} courses | Nodes: {args.num_nodes} | Queries: {args.queries}\n")
    
    # 1. Ground Truth
    searcher = MonolithicSearcher(dataset=args.dataset)
    # Take a subset so DHT injection doesn't take hours
    subset_courses = searcher.courses[:args.dataset_size]
    searcher.courses = subset_courses
    
    test_courses = random.sample(subset_courses, min(args.queries, len(subset_courses)))
    
    ground_truth = []
    print("Pre-computing Monolithic Ground Truth...")
    for c in test_courses:
        q_text = f"{c['course_title']} {c['category']} {c['description']}"
        results, latency = run_monolithic(searcher, q_text)
        
        gt_ids = [res['course_id'] for sim, res in results]
        ground_truth.append({"course": c, "gt_ids": gt_ids, "mono_latency": latency})
    
    nprobes_to_test = [1, 2, 3, 4, 5]
    benchmark_results = {
        "standard_dht": {"recall": [], "hops": [], "latency": []},
        "clustered_dht": {
            f"nprobe_{n}": {"recall": [], "hops": [], "latency": []} for n in nprobes_to_test
        },
        "semantic_router": {
            f"nprobe_{n}": {"recall": [], "hops": [], "latency": []} for n in nprobes_to_test
        }
    }

    # Helper function to run tests for an architecture
    def evaluate_arch(arch_name, setup_fn, teardown_fn, base_port, is_semantic=False):
        print(f"--- Evaluating {arch_name} ---")
        nodes = setup_fn(num_nodes=args.num_nodes, base_port=base_port, dataset=args.dataset)
        
        print("Waiting 10s for additional ring stabilization...")
        time.sleep(10)
        
        inject_data(nodes, subset_courses, arch_name)
        
        client = xmlrpc.client.ServerProxy(f"http://{nodes[0].address}")
        
        use_nprobe = (is_semantic or arch_name == "clustered_dht")
        
        for gt in ground_truth:
            c_json = json.dumps(gt["course"])
            
            nprobes = nprobes_to_test if use_nprobe else [None]
            for np in nprobes:
                (res_tuple, hops), latency = run_dht_query(client, c_json, np)
                
                # res_tuple is a list of json strings
                retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                recall = compute_recall(retrieved_ids, gt["gt_ids"])
                
                key = f"nprobe_{np}" if use_nprobe else arch_name
                target_dict = benchmark_results[arch_name][key] if use_nprobe else benchmark_results[key]
                
                target_dict["recall"].append(recall)
                target_dict["hops"].append(hops)
                target_dict["latency"].append(latency)
                
        teardown_fn(nodes)
        print(f"{arch_name} complete.\n")

    # Run evaluations
    evaluate_arch("standard_dht", setup_standard, teardown_standard, 8100)
    evaluate_arch("clustered_dht", setup_clustered, teardown_clustered, 8200)
    evaluate_arch("semantic_router", setup_semantic, teardown_semantic, 8300, is_semantic=True)

    # Save results
    config = load_config()
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    out_dir = os.path.join(project_root, "data", "benchmarks")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "results.json")
    
    with open(out_path, "w") as f:
        json.dump(benchmark_results, f, indent=4)
        
    print(f"Benchmarks finished successfully! Results saved to {out_path}")

if __name__ == "__main__":
    main()
