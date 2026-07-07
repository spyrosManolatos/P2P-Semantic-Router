import os
import json
import matplotlib.pyplot as plt
import numpy as np
import sys

def main():
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    results_path = os.path.join(project_root, "data", "benchmarks", "results.json")
    
    if not os.path.exists(results_path):
        print(f"Error: Could not find results at {results_path}")
        return
        
    with open(results_path, "r") as f:
        data = json.load(f)
        
    # Aggregate means
    def get_means(d):
        return {
            "recall": np.mean(d["recall"]) * 100, # to percentage
            "hops": np.mean(d["hops"]),
            "latency": np.mean(d["latency"])
        }
        
    standard = get_means(data["standard_dht"])
    
    # Dynamically parse all evaluated nprobes from keys
    nprobes = sorted([int(k.split("_")[1]) for k in data["semantic_router"].keys()])
    
    clustered = {
        n: get_means(data["clustered_dht"][f"nprobe_{n}"])
        for n in nprobes
    }
    semantic = {
        n: get_means(data["semantic_router"][f"nprobe_{n}"])
        for n in nprobes
    }
    
    out_dir = os.path.join(project_root, "data", "benchmarks")
    os.makedirs(out_dir, exist_ok=True)
    
    # 1. Recall vs nprobe
    clustered_recalls = [clustered[n]["recall"] for n in nprobes]
    semantic_recalls = [semantic[n]["recall"] for n in nprobes]
    
    plt.figure(figsize=(8, 5))
    plt.plot(nprobes, semantic_recalls, marker='o', linestyle='-', color='b', label='Semantic Router')
    plt.plot(nprobes, clustered_recalls, marker='s', linestyle='-.', color='g', label='Clustered DHT')
    plt.axhline(y=standard["recall"], color='r', linestyle='--', label='Standard DHT')
    plt.xlabel("nprobe (Number of clusters queried)")
    plt.ylabel("Recall (%)")
    plt.title("Search Recall by Architecture")
    plt.xticks(nprobes)
    plt.legend()
    plt.grid(True)
    plt.savefig(os.path.join(out_dir, "recall_vs_nprobe.png"))
    plt.close()
    
    # 2. Hops vs nprobe
    clustered_hops = [clustered[n]["hops"] for n in nprobes]
    semantic_hops = [semantic[n]["hops"] for n in nprobes]
    
    plt.figure(figsize=(8, 5))
    plt.plot(nprobes, semantic_hops, marker='o', linestyle='-', color='b', label='Semantic Router')
    plt.plot(nprobes, clustered_hops, marker='s', linestyle='-.', color='g', label='Clustered DHT')
    plt.axhline(y=standard["hops"], color='r', linestyle='--', label='Standard DHT')
    plt.xlabel("nprobe (Number of clusters queried)")
    plt.ylabel("Network Hops (Average)")
    plt.title("Network Routing Efficiency (Hops) by Architecture")
    plt.xticks(nprobes)
    plt.legend()
    plt.grid(True)
    plt.savefig(os.path.join(out_dir, "hops_vs_nprobe.png"))
    plt.close()
 
    # 3. Latency vs nprobe
    clustered_lat = [clustered[n]["latency"] for n in nprobes]
    semantic_lat = [semantic[n]["latency"] for n in nprobes]
    
    plt.figure(figsize=(8, 5))
    plt.plot(nprobes, semantic_lat, marker='o', linestyle='-', color='b', label='Semantic Router')
    plt.plot(nprobes, clustered_lat, marker='s', linestyle='-.', color='g', label='Clustered DHT')
    plt.axhline(y=standard["latency"], color='r', linestyle='--', label='Standard DHT')
    plt.xlabel("nprobe (Number of clusters queried)")
    plt.ylabel("End-to-End Latency (ms)")
    plt.title("End-to-End Search Latency by Architecture")
    plt.xticks(nprobes)
    plt.legend()
    plt.grid(True)
    plt.savefig(os.path.join(out_dir, "latency_vs_nprobe.png"))
    plt.close()
    
    print(f"Generated 3 visualization graphs in {out_dir}/")

if __name__ == "__main__":
    main()
