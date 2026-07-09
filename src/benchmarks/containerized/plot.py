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
results_dir = os.path.join(project_root, "data", "benchmarks", "results", "containerized")
plots_dir = os.path.join(project_root, "data", "benchmarks", "plots", "containerized")

def get_means(d):
    return {
        "recall": np.mean(d["recall"]) * 100,
        "hops": np.mean(d["hops"]),
        "latency": np.mean(d["latency"])
    }

def load_result_file(filename):
    path = os.path.join(results_dir, filename)
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return None

def plot_scale():
    std_data = load_result_file("results_standard.json")
    cls_data = load_result_file("results_clustered.json")
    sem_data = load_result_file("results_semantic.json")
    
    if not (std_data or cls_data or sem_data):
        print("Skipping scaling plots: No results_*.json files found in results/containerized.")
        return

    nprobes = [1, 2, 3, 4, 5]
    mean_monolithic_lat = None
    
    for dataset in [sem_data, cls_data, std_data]:
        if dataset and "monolithic" in dataset:
            mean_monolithic_lat = np.mean(dataset["monolithic"])
            break

    fig, axs = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle("Scaling Metrics vs NProbe", fontsize=15, fontweight='bold')
    
    # 1. Recall vs nprobe
    if sem_data:
        semantic_recalls = [get_means(sem_data["dht_results"][f"nprobe_{n}"])["recall"] for n in nprobes]
        axs[0].plot(nprobes, semantic_recalls, marker='o', linestyle='-', color='b', label='Semantic Router')
    if cls_data:
        clustered_recalls = [get_means(cls_data["dht_results"][f"nprobe_{n}"])["recall"] for n in nprobes]
        axs[0].plot(nprobes, clustered_recalls, marker='s', linestyle='-.', color='g', label='Clustered DHT')
    if std_data:
        standard_recall = get_means(std_data["dht_results"]["standard"])["recall"]
        axs[0].axhline(y=standard_recall, color='r', linestyle='--', label='Standard DHT')
        
    axs[0].set_xlabel("nprobe (Clusters queried)")
    axs[0].set_ylabel("Recall (%)")
    axs[0].set_title("Search Recall")
    axs[0].set_xticks(nprobes)
    axs[0].legend()
    axs[0].grid(True)
    
    # 2. Hops vs nprobe
    if sem_data:
        semantic_hops = [get_means(sem_data["dht_results"][f"nprobe_{n}"])["hops"] for n in nprobes]
        axs[1].plot(nprobes, semantic_hops, marker='o', linestyle='-', color='b', label='Semantic Router')
    if cls_data:
        clustered_hops = [get_means(cls_data["dht_results"][f"nprobe_{n}"])["hops"] for n in nprobes]
        axs[1].plot(nprobes, clustered_hops, marker='s', linestyle='-.', color='g', label='Clustered DHT')
    if std_data:
        standard_hops = get_means(std_data["dht_results"]["standard"])["hops"]
        axs[1].axhline(y=standard_hops, color='r', linestyle='--', label='Standard DHT')
        
    axs[1].set_xlabel("nprobe (Clusters queried)")
    axs[1].set_ylabel("Network Hops")
    axs[1].set_title("Routing Efficiency")
    axs[1].set_xticks(nprobes)
    axs[1].legend()
    axs[1].grid(True)
    
    # 3. Latency vs nprobe
    if sem_data:
        semantic_lat = [get_means(sem_data["dht_results"][f"nprobe_{n}"])["latency"] for n in nprobes]
        axs[2].plot(nprobes, semantic_lat, marker='o', linestyle='-', color='b', label='Semantic Router')
    if cls_data:
        clustered_lat = [get_means(cls_data["dht_results"][f"nprobe_{n}"])["latency"] for n in nprobes]
        axs[2].plot(nprobes, clustered_lat, marker='s', linestyle='-.', color='g', label='Clustered DHT')
    if std_data:
        standard_lat = get_means(std_data["dht_results"]["standard"])["latency"]
        axs[2].axhline(y=standard_lat, color='m', linestyle=':', label='Standard DHT')
    if mean_monolithic_lat is not None:
        axs[2].axhline(y=mean_monolithic_lat, color='r', linestyle='--', label='Monolithic Search')
        
    axs[2].set_xlabel("nprobe (Clusters queried)")
    axs[2].set_ylabel("Latency (ms)")
    axs[2].set_title("End-to-End Latency")
    axs[2].set_xticks(nprobes)
    axs[2].legend()
    axs[2].grid(True)
    
    plt.tight_layout()
    plt.savefig(os.path.join(plots_dir, "scaling_metrics_combined.png"), dpi=300)
    plt.close()
    print("Generated combined scaling metrics plot.")

def plot_fault_tolerance():
    # Plots a combined apples-to-apples comparison of Recall across all architectures
    architectures = ["standard", "clustered", "semantic"]
    labels = ["Standard DHT", "Clustered DHT", "Semantic Router"]
    
    baseline_recalls = []
    after_recalls = []
    
    for arch in architectures:
        results_path = os.path.join(results_dir, f"fault_tolerance_results_{arch}.json")
        if not os.path.exists(results_path):
            baseline_recalls.append(0)
            after_recalls.append(0)
            continue
            
        with open(results_path, "r") as f:
            data = json.load(f)
            
        baseline_recalls.append(data.get("baseline", {}).get("recall", 0) * 100)
        after_recalls.append(data.get("after_failure", {}).get("recall", 0) * 100)
        
    if not any(baseline_recalls):
        return
        
    x = np.arange(len(labels))
    width = 0.35
    
    fig, ax = plt.subplots(figsize=(10, 6))
    rects1 = ax.bar(x - width/2, baseline_recalls, width, label='Before Failure (Baseline)', color='#2ecc71', alpha=0.8)
    rects2 = ax.bar(x + width/2, after_recalls, width, label='After Node Failure (5s wait)', color='#e74c3c', alpha=0.8)
    
    ax.set_ylabel('Recall Accuracy (%)', fontsize=12)
    ax.set_title('Fault Tolerance: Recall Impact by Architecture', fontsize=14, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=11)
    ax.set_ylim(0, 115)
    ax.legend()
    
    # Add data labels
    def autolabel(rects):
        for rect in rects:
            height = rect.get_height()
            ax.annotate(f'{height:.1f}%',
                        xy=(rect.get_x() + rect.get_width() / 2, height),
                        xytext=(0, 3),  # 3 points vertical offset
                        textcoords="offset points",
                        ha='center', va='bottom', fontweight='bold')
                        
    autolabel(rects1)
    autolabel(rects2)
    
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.savefig(os.path.join(plots_dir, "fault_tolerance_combined_recall.png"), dpi=300)
    plt.close()
    print("Generated combined fault tolerance recall plot.")

def plot_node_join():
    architectures = ["standard", "clustered", "semantic"]
    labels = ["Standard DHT", "Clustered DHT", "Semantic Router"]
    
    orig_recalls, exp_recalls = [], []
    orig_hops, exp_hops = [], []
    orig_lats, exp_lats = [], []
    
    for arch in architectures:
        results_path = os.path.join(results_dir, f"node_join_results_{arch}.json")
        if not os.path.exists(results_path):
            orig_recalls.append(0); exp_recalls.append(0)
            orig_hops.append(0); exp_hops.append(0)
            orig_lats.append(0); exp_lats.append(0)
            continue
            
        with open(results_path, "r") as f:
            data = json.load(f)
            
        orig_recalls.append(data["original_5_nodes"]["recall"] * 100)
        exp_recalls.append(data["expanded_6_nodes"]["recall"] * 100)
        
        orig_hops.append(data["original_5_nodes"]["hops"])
        exp_hops.append(data["expanded_6_nodes"]["hops"])
        
        orig_lats.append(data["original_5_nodes"]["latency"])
        exp_lats.append(data["expanded_6_nodes"]["latency"])
        
    if not any(orig_recalls):
        return
        
    fig, axs = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle("Node Join Impact: Dynamic Ring Expansion (5 -> 6 Nodes)", fontsize=15, fontweight='bold')
    
    x = np.arange(len(labels))
    width = 0.35
    
    # Recall
    axs[0].bar(x - width/2, orig_recalls, width, label='Original Ring (5)', color='#3498db')
    axs[0].bar(x + width/2, exp_recalls, width, label='Expanded Ring (6)', color='#2980b9')
    axs[0].set_ylabel('Recall (%)')
    axs[0].set_title('Search Recall Accuracy')
    axs[0].set_xticks(x)
    axs[0].set_xticklabels(labels)
    axs[0].set_ylim(0, 115)
    axs[0].legend()
    
    # Hops
    axs[1].bar(x - width/2, orig_hops, width, label='Original Ring (5)', color='#e67e22')
    axs[1].bar(x + width/2, exp_hops, width, label='Expanded Ring (6)', color='#d35400')
    axs[1].set_ylabel('Network Hops')
    axs[1].set_title('Average Routing Hops')
    axs[1].set_xticks(x)
    axs[1].set_xticklabels(labels)
    axs[1].legend()
    
    # Latency
    axs[2].bar(x - width/2, orig_lats, width, label='Original Ring (5)', color='#2ecc71')
    axs[2].bar(x + width/2, exp_lats, width, label='Expanded Ring (6)', color='#27ae60')
    axs[2].set_ylabel('Latency (ms)')
    axs[2].set_title('End-to-End Latency')
    axs[2].set_xticks(x)
    axs[2].set_xticklabels(labels)
    axs[2].legend()
    
    plt.tight_layout()
    plt.savefig(os.path.join(plots_dir, "node_join_combined.png"), dpi=300)
    plt.close()
    print("Generated combined node join metrics plot.")

def plot_load_balancing():
    sem_path = os.path.join(results_dir, "load_balancing_results_semantic.json")
    if not os.path.exists(sem_path):
        return
        
    with open(sem_path, "r") as f:
        data = json.load(f)
        
    concurrency = data["concurrency_levels"]
    with_lb = data["with_load_balancing"]
    without_lb = data["without_load_balancing"]
    
    plt.figure(figsize=(8, 5))
    plt.plot(concurrency, with_lb, marker='o', linestyle='-', color='#1a5f7a', linewidth=2, label='Active Replica Delegation (ON)')
    plt.plot(concurrency, without_lb, marker='s', linestyle='--', color='#c0392b', linewidth=2, label='No Load Balancing (OFF)')
    
    plt.xlabel("Query Concurrency Level (Concurrent Threads)")
    plt.ylabel("Mean Latency per Request (ms)")
    plt.title("Concurrency Performance & Load Mitigation")
    plt.xticks(concurrency)
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(plots_dir, "load_balancing_metrics.png"), dpi=300)
    plt.close()
    print("Generated load balancing metrics plot.")

def plot_disaster_scenario():
    architectures = ["clustered", "semantic"]
    labels = ["Clustered DHT\n(Sparse/Scattered)", "Semantic Router\n(Dense/Contiguous)"]
    
    baseline_recalls = []
    disaster_recalls = []
    
    for arch in architectures:
        results_path = os.path.join(results_dir, f"disaster_results_{arch}.json")
        if not os.path.exists(results_path):
            baseline_recalls.append(0)
            disaster_recalls.append(0)
            continue
            
        with open(results_path, "r") as f:
            data = json.load(f)
            
        baseline_recalls.append(data["baseline_recall"] * 100)
        disaster_recalls.append(data["disaster_recall"] * 100)
        
    if not any(baseline_recalls):
        return
        
    fig, ax = plt.subplots(figsize=(8, 6))
    
    x = np.arange(len(labels))
    width = 0.35
    
    rects1 = ax.bar(x - width/2, baseline_recalls, width, label='Baseline (Healthy Ring)', color='#9b59b6')
    rects2 = ax.bar(x + width/2, disaster_recalls, width, label='Disaster (Primary Node Assasinated)', color='#e74c3c')
    
    ax.set_ylabel('Recall (%)', fontsize=12)
    ax.set_title('Disaster Scenario: Targeted Primary Node Failure (nprobe=5)', fontsize=14, fontweight='bold')
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=12)
    ax.set_ylim(0, 115)
    ax.legend(fontsize=11)
    
    # Add data labels
    def autolabel(rects):
        for rect in rects:
            height = rect.get_height()
            ax.annotate(f'{height:.1f}%',
                        xy=(rect.get_x() + rect.get_width() / 2, height),
                        xytext=(0, 3),  # 3 points vertical offset
                        textcoords="offset points",
                        ha='center', va='bottom', fontweight='bold', fontsize=11)
                        
    autolabel(rects1)
    autolabel(rects2)
    
    plt.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.savefig(os.path.join(plots_dir, "disaster_scenario_comparison.png"), dpi=300)
    plt.close()
    print("Generated disaster scenario comparison plot.")

def main():
    os.makedirs(plots_dir, exist_ok=True)
    plot_scale()
    plot_fault_tolerance()
    plot_node_join()
    plot_load_balancing()
    plot_disaster_scenario()

if __name__ == "__main__":
    main()
