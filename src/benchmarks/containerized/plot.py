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

import sys
project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
src_dir = os.path.join(project_root, "src")
if src_dir not in sys.path:
    sys.path.append(src_dir)
results_dir = os.path.join(project_root, "data", "benchmarks", "results", "containerized")
plots_dir = os.path.join(project_root, "data", "benchmarks", "plots", "containerized")

# Reported scope is async-only: the two architectures compared everywhere are the
# semantic router and the clustered DHT (the sync XML-RPC stacks and the standard
# DHT baseline were retired -- see docs/scripts.md). Every plot reads the
# *_async_* result files produced by evaluate.py.

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

def _nprobes_from(*datasets):
    # Derive the nprobe sweep from the data itself -- the async sweep is not a
    # contiguous 1..5 range (e.g. [1,2,3,5,8,12,20,40,80]), so a hardcoded list
    # would KeyError. Use the first dataset that carries a dht_results block.
    for d in datasets:
        if d and "dht_results" in d:
            ns = sorted(int(k.split("_")[1]) for k in d["dht_results"] if k.startswith("nprobe_"))
            if ns:
                return ns
    return []

def plot_scale():
    cls_data = load_result_file("results_async_clustered.json")
    sem_data = load_result_file("results_async_semantic.json")

    if not (cls_data or sem_data):
        print("Skipping scaling plots: No results_async_*.json files found in results/containerized.")
        return

    nprobes = _nprobes_from(sem_data, cls_data)
    if not nprobes:
        print("Skipping scaling plots: no nprobe_* entries in dht_results.")
        return

    mean_monolithic_lat = None
    for dataset in [sem_data, cls_data]:
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
    # Plots an apples-to-apples comparison across both architectures for THREE
    # metrics (recall, average hops, end-to-end latency), each under the
    # two-point measurement: baseline -> transient dip -> healed recovery.
    architectures = ["async_clustered", "async_semantic"]
    labels = ["Clustered DHT", "Semantic Router"]

    # metric_key -> per-state list across architectures
    metrics = {
        "recall":  {"baseline": [], "transient": [], "healed": []},
        "hops":    {"baseline": [], "transient": [], "healed": []},
        "latency": {"baseline": [], "transient": [], "healed": []},
    }
    nprobe_used = None
    have_data = False

    for arch in architectures:
        results_path = os.path.join(results_dir, f"fault_tolerance_results_{arch}.json")
        if not os.path.exists(results_path):
            for m in metrics.values():
                for s in m:
                    m[s].append(0)
            continue

        have_data = True
        with open(results_path, "r") as f:
            data = json.load(f)

        base = data.get("baseline", {})
        # Two-point measurement: transient (dip) then healed (recovery).
        trans = data.get("after_failure_transient", data.get("after_failure", {}))
        heal = data.get("after_failure_healed", {})

        # recall stored as fraction -> percentage
        metrics["recall"]["baseline"].append(base.get("recall", 0) * 100)
        metrics["recall"]["transient"].append(trans.get("recall", 0) * 100)
        metrics["recall"]["healed"].append(heal.get("recall", 0) * 100)

        metrics["hops"]["baseline"].append(base.get("hops", 0))
        metrics["hops"]["transient"].append(trans.get("hops", 0))
        metrics["hops"]["healed"].append(heal.get("hops", 0))

        metrics["latency"]["baseline"].append(base.get("latency", 0))
        metrics["latency"]["transient"].append(trans.get("latency", 0))
        metrics["latency"]["healed"].append(heal.get("latency", 0))

        if "nprobe" in data:
            nprobe_used = data["nprobe"]

    if not have_data:
        return

    nprobe_str = f" (fixed nprobe={nprobe_used})" if nprobe_used is not None else ""

    x = np.arange(len(labels))
    width = 0.27
    state_style = [
        ("baseline",  "Before Failure (Baseline)",   "#2ecc71"),
        ("transient", "Transient (5s, mid-recovery)", "#e74c3c"),
        ("healed",    "Healed (after promotion)",     "#3498db"),
    ]

    panels = [
        ("recall",  "Recall Accuracy (%)",   "{:.0f}%",  "Recall"),
        ("hops",    "Avg. Routing Hops",     "{:.2f}",   "Routing Hops"),
        ("latency", "End-to-End Latency (ms, relative)", "{:.0f}", "Latency"),
    ]

    fig, axes = plt.subplots(1, 3, figsize=(20, 6))

    for ax, (mkey, ylabel, fmt, subtitle) in zip(axes, panels):
        m = metrics[mkey]
        rects_groups = []
        for (skey, slabel, scolor), off in zip(state_style, (-width, 0.0, width)):
            rects = ax.bar(x + off, m[skey], width, label=slabel, color=scolor, alpha=0.85)
            rects_groups.append(rects)

        ax.set_ylabel(ylabel, fontsize=12)
        ax.set_title(subtitle, fontsize=13, fontweight='bold')
        ax.set_xticks(x)
        ax.set_xticklabels(labels, fontsize=10, rotation=10)
        ax.grid(axis='y', linestyle='--', alpha=0.7)

        # headroom for data labels
        top = max([v for grp in rects_groups for v in [r.get_height() for r in grp]] + [1])
        ax.set_ylim(0, top * 1.18)

        for rects in rects_groups:
            for rect in rects:
                height = rect.get_height()
                ax.annotate(fmt.format(height),
                            xy=(rect.get_x() + rect.get_width() / 2, height),
                            xytext=(0, 3), textcoords="offset points",
                            ha='center', va='bottom', fontweight='bold', fontsize=8)

    # single shared legend + supertitle
    handles, leg_labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, leg_labels, loc='upper center', ncol=3, fontsize=11,
               bbox_to_anchor=(0.5, 0.99))
    fig.suptitle(f'Fault Tolerance: Transient Dip and Recovery{nprobe_str}',
                 fontsize=15, fontweight='bold', y=1.04)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(os.path.join(plots_dir, "fault_tolerance_combined_recall.png"),
                dpi=300, bbox_inches='tight')
    plt.close()
    print("Generated combined fault tolerance plot (recall + hops + latency).")

def plot_node_join():
    architectures = ["async_clustered", "async_semantic"]
    labels = ["Clustered DHT", "Semantic Router"]

    orig_recalls, exp_recalls = [], []
    orig_hops, exp_hops = [], []
    orig_lats, exp_lats = [], []
    nprobe_used = None

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
        if "nprobe" in data:
            nprobe_used = data["nprobe"]

    if not any(orig_recalls):
        return

    nprobe_str = f" (fixed nprobe={nprobe_used})" if nprobe_used is not None else ""
    fig, axs = plt.subplots(1, 3, figsize=(18, 5))
    fig.suptitle(f"Node Join Impact: Dynamic Ring Expansion (5 -> 6 Nodes){nprobe_str}", fontsize=15, fontweight='bold')

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

def plot_disaster_scenario():
    # The small-ring (5-node) correlated-failure contrast: kill RF+1 adjacent
    # nodes, measure recall before/after. This is the sparse-regime data point;
    # the dense (100-vnode) uniform-disaster DISTRIBUTION is a separate study
    # (disaster_results_vnode_async_*.json) not summarised as a single bar.
    architectures = ["async_clustered", "async_semantic"]
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
    rects2 = ax.bar(x + width/2, disaster_recalls, width, label='Disaster (Primary Node Failure)', color='#e74c3c')

    ax.set_ylabel('Recall (%)', fontsize=12)
    ax.set_title('Disaster Scenario: Targeted Primary Node Failure', fontsize=14, fontweight='bold')
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

def plot_hops_sweep():
    # Virtual-node scaling experiment: routing hops vs nprobe (the win) + recall
    # sanity line. Semantic fanout stays ~flat (adjacent clusters, same/neighbor
    # node); clustered grows ~linearly (a fresh lookup per scattered cluster).
    # Coincident-line safety: the two recall curves can overlap EXACTLY (identical
    # cluster sets retrieved), so each series carries its own linestyle + marker as
    # secondary encoding -- identity never rests on color alone, and a dashed line
    # drawn on top of a solid one stays visible under perfect overlap.
    arch_style = [
        # (arch, label, color, linestyle, marker, zorder)
        ("async_clustered", "Clustered DHT",   "#3498db", "-",  "s", 2),
        ("async_semantic",  "Semantic Router", "#e74c3c", "--", "o", 3),
    ]
    num_nodes = None
    fig, (axh, axr) = plt.subplots(1, 2, figsize=(15, 6))
    plotted = False
    recall_pts = []  # all plotted recall %s, for a data-driven (compact) y-range

    for arch, label, color, ls, mk, zo in arch_style:
        path = os.path.join(results_dir, f"hops_sweep_results_{arch}.json")
        if not os.path.exists(path):
            continue
        with open(path, "r") as f:
            data = json.load(f)
        x = data["nprobe_values"]
        num_nodes = data.get("num_nodes", num_nodes)
        rpct = [r * 100 for r in data["recall"]]
        axh.plot(x, data["hops"], marker=mk, markersize=7, linewidth=2.2,
                 color=color, linestyle=ls, zorder=zo, label=label)
        axr.plot(x, rpct, marker=mk, markersize=7, linewidth=2.2,
                 color=color, linestyle=ls, zorder=zo, label=label)
        recall_pts.extend(rpct)
        # Selective direct label: endpoint value only (hops panel).
        axh.annotate(f"{data['hops'][-1]:.1f}", xy=(x[-1], data["hops"][-1]),
                     xytext=(6, 0), textcoords="offset points",
                     va="center", fontsize=10, fontweight="bold", color="#333333")
        plotted = True

    if not plotted:
        return

    title_n = f" — {num_nodes}-node ring" if num_nodes else ""
    axh.set_xlabel("nprobe (fanout width)", fontsize=12)
    axh.set_ylabel("Total routing hops per query (cumulative)", fontsize=12)
    axh.set_title("Routing hops per query", fontsize=13, fontweight="bold")
    axh.grid(True, linestyle="--", alpha=0.7)
    axh.legend(fontsize=10)

    axr.set_xlabel("nprobe (fanout width)", fontsize=12)
    axr.set_ylabel("Recall@5 (%)", fontsize=12)
    axr.set_title("Recall@5", fontsize=13, fontweight="bold")
    # Compact, data-driven y-range so the small recall climb is visible (a 0-100
    # axis flattens it). Pad a few points on each side, clamp to [0,100].
    if recall_pts:
        lo = max(0, min(recall_pts) - 4)
        hi = min(100, max(recall_pts) + 4)
        if hi - lo < 8:  # avoid an over-tight range on nearly-flat data
            mid = (hi + lo) / 2
            lo, hi = max(0, mid - 4), min(100, mid + 4)
        axr.set_ylim(lo, hi)
    axr.grid(True, linestyle="--", alpha=0.7)
    axr.legend(fontsize=10)

    fig.suptitle(f"Fanout routing cost and recall vs nprobe{title_n}",
                 fontsize=14, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.96])
    plt.savefig(os.path.join(plots_dir, "hops_vs_nprobe.png"), dpi=300, bbox_inches="tight")
    plt.close()
    print("Generated virtual-node hops-vs-nprobe plot.")

def plot_vnode_disaster():
    # Dense-ring (100-vnode) uniform-query disaster: kill the heaviest primary
    # node, measure the DISTRIBUTION of recall loss across 500 uniform queries.
    # The headline is the SHAPE: semantic fails bimodally (a whole topic
    # community -> 0), clustered fails diffusely (fewer hit, graded).
    styles = [
        ("async_semantic",  "Semantic Router", "#e74c3c"),
        ("async_clustered", "Clustered DHT",   "#3498db"),
    ]
    loaded = []
    for arch, label, color in styles:
        d = load_result_file(f"disaster_results_vnode_{arch}.json")
        if not d:
            d = load_result_file(f"disaster_results_vnode_{arch}.100nodes_uniform500.json")
        if d:
            loaded.append((label, color, d))
    if not loaded:
        print("Skipping vnode disaster plot: No disaster_results_vnode_*.json files found.")
        return

    fig, (axc, axb) = plt.subplots(1, 2, figsize=(15, 6))

    # Panel A: per-cluster recall LOST, clusters ranked worst-first (one line per
    # architecture). Monotonic by construction -> no small-n noise. It shows how
    # many topic-clusters lost recall and by how much: semantic's curve is high
    # and long (a big contiguous block near-fully wiped), clustered's is lower and
    # shorter (fewer clusters, partial loss). Both fall to 0 -> every other cluster
    # is untouched.
    max_aff = 1
    for label, color, d in loaded:
        drops = sorted((c["mean_drop"] * 100 for c in d["per_cluster"].values()), reverse=True)
        n_aff = sum(1 for v in drops if v > 1e-6)
        max_aff = max(max_aff, n_aff)
        xs = list(range(1, len(drops) + 1))
        axc.plot(xs, drops, "-", color=color, linewidth=2.4, label=f"{label} ({n_aff} clusters lose recall)")
        axc.fill_between(xs, 0, drops, color=color, alpha=0.15)

    axc.set_xlim(1, max_aff)
    axc.set_ylim(0, 105)
    axc.set_xlabel("Topic-cluster, ranked by recall lost (worst \u2192 least)", fontsize=12)
    axc.set_ylabel("Recall@5 lost (percentage points)", fontsize=12)
    axc.set_title("Which clusters lose recall (and how much)", fontsize=13, fontweight="bold")
    axc.legend(fontsize=10)
    axc.grid(True, linestyle="--", alpha=0.7)

    # Panel B: headline summary bars (mean drop pp, % affected, recovery).
    metrics = ["Mean drop\n(all queries, pp)", "Queries\naffected (%)", "Home-cluster\nrecall after (%)"]
    x = np.arange(len(metrics))
    width = 0.36
    for i, (label, color, d) in enumerate(loaded):
        conc = d["concentration"]
        reg = d["region_summary"]["in_killed"]
        vals = [conc["mean_drop_all"] * 100, conc["frac_affected"] * 100, reg["mean_disaster"] * 100]
        off = (-width / 2) if i == 0 else (width / 2)
        rects = axb.bar(x + off, vals, width, label=label, color=color, alpha=0.85)
        for r in rects:
            axb.annotate(f"{r.get_height():.1f}", xy=(r.get_x() + r.get_width() / 2, r.get_height()),
                         xytext=(0, 3), textcoords="offset points", ha="center", va="bottom",
                         fontsize=9, fontweight="bold")
    axb.set_xticks(x)
    axb.set_xticklabels(metrics, fontsize=10)
    axb.set_ylabel("Percentage points / Percent", fontsize=12)
    axb.set_title("Blast radius and recovery (nprobe=5)", fontsize=13, fontweight="bold")
    axb.set_ylim(0, 105)
    axb.legend(fontsize=10)
    axb.grid(axis="y", linestyle="--", alpha=0.7)

    fig.suptitle("Dense-ring hot-node disaster: correlated topic outage (Semantic) vs diffuse degradation (Clustered)",
                 fontsize=13, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.95])
    plt.savefig(os.path.join(plots_dir, "vnode_disaster_distribution.png"), dpi=300, bbox_inches="tight")
    plt.close()
    print("Generated dense-ring disaster distribution plot.")

def plot_doomed():
    styles = [
        ("async_clustered", "Clustered DHT",   "#3498db", "s"),
        ("async_semantic",  "Semantic Router", "#e74c3c", "o"),
    ]
    fig, (axr, axh) = plt.subplots(1, 2, figsize=(15, 6))
    plotted = False
    for arch, label, color, mk in styles:
        d = load_result_file(f"doomed_scenario_results_{arch}.json")
        if not d:
            continue
        x = d["nprobe_values"]
        br = [r * 100 for r in d["baseline_recall_by_nprobe"]]
        dr = [r * 100 for r in d["doomed_recall_by_nprobe"]]
        bh = d["baseline_hops_by_nprobe"]
        # keep only non-timeout points (hops>0); clustered records 0 on timeout.
        keep = [i for i, h in enumerate(bh) if h > 0]
        if not keep:
            continue
        xs = [x[i] for i in keep]; brs = [br[i] for i in keep]
        drs = [dr[i] for i in keep]; hs = [bh[i] for i in keep]
        # Recall panel: healthy baseline (solid) vs doomed-query recall after the
        # cluster is killed (dashed, hollow marker). The shaded gap = recall lost
        # to the doom -- the doomed queries collapse to ~0 for both architectures.
        axr.plot(xs, brs, marker=mk, color=color, linestyle="-", linewidth=2.2,
                 label=f"{label} — healthy baseline")
        axr.plot(xs, drs, marker=mk, markerfacecolor="white", color=color,
                 linestyle="--", linewidth=2.0, label=f"{label} — doomed (cluster killed)")
        axr.fill_between(xs, drs, brs, color=color, alpha=0.12)
        axh.plot(xs, hs, marker=mk, color=color, linestyle="-", linewidth=2.2, label=label)
        if len(xs) < len(x):
            axh.annotate("times out →", xy=(xs[-1], hs[-1]), xytext=(6, 6),
                         textcoords="offset points", va="bottom", fontsize=9,
                         fontweight="bold", color=color)
        plotted = True
    if not plotted:
        return
    axr.set_xlabel("nprobe (fanout width)", fontsize=12)
    axr.set_ylabel("Recall@5 (%)", fontsize=12)
    axr.set_title("Healthy baseline vs doomed recall (shaded = recall lost to the doom)",
                  fontsize=11.5, fontweight="bold")
    axr.set_ylim(-3, None)
    axr.grid(True, linestyle="--", alpha=0.7); axr.legend(fontsize=9)
    axh.set_xlabel("nprobe (fanout width)", fontsize=12)
    axh.set_ylabel("Routing hops per query", fontsize=12)
    axh.set_title("Routing cost vs nprobe", fontsize=13, fontweight="bold")
    axh.grid(True, linestyle="--", alpha=0.7); axh.legend(fontsize=10)
    fig.suptitle("Doomed scenario (100 nodes): doomed queries collapse to ~0% recall; clustered's hops also explode and time out",
                 fontsize=12, fontweight="bold")
    plt.tight_layout(rect=[0, 0, 1, 0.95])
    plt.savefig(os.path.join(plots_dir, "doomed_scenario.png"), dpi=300, bbox_inches="tight")
    plt.close()
    print("Generated doomed scenario plot.")

def plot_c_ladder_figure():
    ladder_dir = os.path.join(results_dir, "ladder")
    if os.path.exists(ladder_dir) and any(f.startswith("prod.") for f in os.listdir(ladder_dir)):
        try:
            from benchmarks.containerized import plot_c_ladder
            plot_c_ladder.main()
        except Exception as e:
            print(f"Could not generate c-ladder plot: {e}")

def main():
    os.makedirs(plots_dir, exist_ok=True)
    plot_scale()
    plot_fault_tolerance()
    plot_node_join()
    plot_disaster_scenario()
    plot_hops_sweep()
    plot_vnode_disaster()
    plot_doomed()
    plot_c_ladder_figure()

if __name__ == "__main__":
    main()
