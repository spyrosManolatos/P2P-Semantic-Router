import os
import json
import matplotlib.pyplot as plt
import numpy as np

# Set design styles for clean, modern academic plots
plt.style.use('seaborn-v0_8-whitegrid' if 'seaborn-v0_8-whitegrid' in plt.style.available else 'default')
plt.rcParams.update({
    'font.size': 11,
    'axes.labelsize': 12,
    'axes.titlesize': 13,
    'xtick.labelsize': 10,
    'ytick.labelsize': 10,
    'figure.titlesize': 14,
    'font.family': 'sans-serif'
})

project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
results_dir = os.path.join(project_root, "data", "benchmarks", "results", "local")
plots_dir = os.path.join(project_root, "data", "benchmarks", "plots", "local")

def plot_scale():
    results_path = os.path.join(results_dir, "results.json")
    if not os.path.exists(results_path):
        print(f"Skipping scaling plots: {results_path} not found.")
        return
        
    with open(results_path, "r") as f:
        data = json.load(f)
        
    def get_means(d):
        return {
            "recall": np.mean(d["recall"]) * 100,
            "hops": np.mean(d["hops"]),
            "latency": np.mean(d["latency"])
        }
        
    standard = get_means(data["standard_dht"])
    nprobes = sorted([int(k.split("_")[1]) for k in data["semantic_router"].keys()])
    
    clustered = {n: get_means(data["clustered_dht"][f"nprobe_{n}"]) for n in nprobes}
    semantic = {n: get_means(data["semantic_router"][f"nprobe_{n}"]) for n in nprobes}
    
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
    plt.savefig(os.path.join(plots_dir, "recall_vs_nprobe.png"), dpi=300)
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
    plt.savefig(os.path.join(plots_dir, "hops_vs_nprobe.png"), dpi=300)
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
    plt.savefig(os.path.join(plots_dir, "latency_vs_nprobe.png"), dpi=300)
    plt.close()
    print("Generated 3 scaling visualization graphs.")

def plot_fault_tolerance():
    results_path = os.path.join(results_dir, "fault_tolerance_results.json")
    if not os.path.exists(results_path):
        print(f"Skipping fault tolerance plots: {results_path} not found.")
        return
        
    with open(results_path, "r") as f:
        data = json.load(f)
        
    states = list(data.keys())
    labels = [s.split(" (")[0] for s in states] 
    
    recalls = [data[s]["recall"] * 100 for s in states]
    hops = [data[s]["hops"] for s in states]
    latencies = [data[s]["latency"] for s in states]
    heal_times = [data[s]["heal_time_sec"] for s in states]
    
    fig, axs = plt.subplots(2, 2, figsize=(12, 9))
    fig.suptitle("Fault Tolerance & Dynamic Self-Healing Performance (RF=3)", y=0.98, fontsize=15, fontweight='bold')
    
    # 1. Search Recall
    axs[0, 0].set_title("Search Recall Accuracy")
    axs[0, 0].bar(labels, recalls, color='#1f77b4', alpha=0.7, width=0.4)
    axs[0, 0].set_ylabel("Recall (%)")
    axs[0, 0].set_ylim(0, 110)
    for i, v in enumerate(recalls):
        axs[0, 0].text(i, v + 2, f"{v:.1f}%", ha='center', fontweight='bold')
        
    # 2. Average Hops
    axs[0, 1].set_title("Average Routing Hops")
    axs[0, 1].plot(labels, hops, color='#ff7f0e', marker='o', linewidth=2.5, markersize=8)
    axs[0, 1].set_ylabel("Hops")
    axs[0, 1].set_ylim(0, max(hops) + 1 if hops else 5)
    for i, v in enumerate(hops):
        axs[0, 1].text(i, v + 0.1, f"{v:.1f}", ha='center', fontweight='bold')
        
    # 3. Query Latency
    axs[1, 0].set_title("Query Latency")
    axs[1, 0].bar(labels, latencies, color='#2ca02c', alpha=0.7, width=0.4)
    axs[1, 0].set_ylabel("Latency (ms)")
    axs[1, 0].set_ylim(0, max(latencies) * 1.2 if latencies else 500)
    for i, v in enumerate(latencies):
        axs[1, 0].text(i, v + 10, f"{v:.1f} ms", ha='center', fontweight='bold')
        
    # 4. Self-Healing Time
    axs[1, 1].set_title("Self-Healing Stabilization Duration")
    axs[1, 1].bar(labels, heal_times, color='#d62728', alpha=0.7, width=0.4)
    axs[1, 1].set_ylabel("Healing Time (seconds)")
    axs[1, 1].set_ylim(0, max(heal_times) * 1.2 if heal_times else 5)
    for i, v in enumerate(heal_times):
        axs[1, 1].text(i, v + 0.05, f"{v:.2f} s", ha='center', fontweight='bold')
        
    plt.tight_layout()
    out_path = os.path.join(plots_dir, "fault_tolerance_metrics.png")
    plt.savefig(out_path, dpi=300)
    plt.close()
    print("Generated fault tolerance metrics plot.")

def plot_node_join():
    results_path = os.path.join(results_dir, "node_join_results.json")
    if not os.path.exists(results_path):
        print(f"Skipping node join plots: {results_path} not found.")
        return
        
    with open(results_path, "r") as f:
        data = json.load(f)
        
    states = ["baseline_5_nodes", "expanded_6_nodes"]
    labels = ["5 Nodes (Baseline)", "6 Nodes (Expanded)"]
    
    recalls = [data[s]["recall"] * 100 for s in states]
    hops = [data[s]["hops"] for s in states]
    latencies = [data[s]["latency"] for s in states]
    migration_time = data["expanded_6_nodes"]["migration_time_sec"]
    
    fig, (ax1, ax2, ax3) = plt.subplots(1, 3, figsize=(15, 5))
    fig.suptitle(f"Network Expansion Metrics (Dynamic Node Join | Migration Time: {migration_time:.2f}s)", y=0.98)
    
    # Recall Compare
    ax1.set_title("Search Recall Accuracy")
    ax1.bar(labels, recalls, color='#1f77b4', alpha=0.7, width=0.4)
    ax1.set_ylabel("Recall (%)")
    ax1.set_ylim(0, 110)
    for i, v in enumerate(recalls):
        ax1.text(i, v + 2, f"{v:.1f}%", ha='center', fontweight='bold')
        
    # Hops Compare
    ax2.set_title("Average Routing Hops")
    ax2.bar(labels, hops, color='#ff7f0e', alpha=0.7, width=0.4)
    ax2.set_ylabel("Hops")
    ax2.set_ylim(0, max(hops) + 1 if hops else 5)
    for i, v in enumerate(hops):
        ax2.text(i, v + 0.1, f"{v:.1f}", ha='center', fontweight='bold')
        
    # Latency Compare
    ax3.set_title("Query Latency")
    ax3.bar(labels, latencies, color='#2ca02c', alpha=0.7, width=0.4)
    ax3.set_ylabel("Latency (ms)")
    ax3.set_ylim(0, max(latencies) * 1.2 if latencies else 500)
    for i, v in enumerate(latencies):
        ax3.text(i, v + 10, f"{v:.1f} ms", ha='center', fontweight='bold')
        
    plt.tight_layout()
    out_path = os.path.join(plots_dir, "node_join_metrics.png")
    plt.savefig(out_path, dpi=300)
    plt.close()
    print("Generated node join metrics plot.")

def main():
    os.makedirs(results_dir, exist_ok=True)
    os.makedirs(plots_dir, exist_ok=True)
    plot_scale()
    plot_fault_tolerance()
    plot_node_join()

if __name__ == "__main__":
    main()
