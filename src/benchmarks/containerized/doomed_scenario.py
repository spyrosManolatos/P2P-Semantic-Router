"""
The Doomed Scenario: a deliberate, guaranteed-worst-case disaster test.

Unlike run_vnode_disaster_scenario (which picks an arbitrary query as the
disaster epicenter and measures recall across a random 50-query sample, most
of which are unaffected by chance -- pure luck of the draw), this experiment:

  1. Deliberately targets the LARGEST K-Means cluster in the corpus. Killing
     its owner destroys the most content possible -- not cherry-picking for
     shock value, but reflecting a real property of course corpora: a few
     topics (e.g. "web development", "Excel") are wildly oversubscribed, so a
     correlated failure landing on the most popular topic is a plausible
     worst case, not a contrived one.
  2. Constructs test queries FROM COURSES THAT ARE THEMSELVES MEMBERS of that
     doomed cluster, so their true top-1 match is guaranteed to be inside it.
     At nprobe=1, once the cluster is destroyed, recall is guaranteed 0% --
     no luck-dependent sampling needed to get a signal.
  3. Sweeps nprobe much higher than the other experiments. Semantic's O(1) hop
     cost (already established by the headline hops-scaling result) means
     pushing nprobe way up costs it almost nothing, unlike clustered whose
     hops grow with nprobe -- so this reports hops alongside recall, for a
     fair "recall recovered per hop spent" comparison, not raw recall-vs-nprobe.

Verified empirically beforehand (manual replica_diag.py run against a live
10-node ring) that killing RF+1=3 consecutive nodes permanently destroys ONLY
the first (target) node's primary data -- the 2nd/3rd killed nodes' data
survives completely intact via replicas already placed on other survivors
BEFORE the kill. So any recall recovery here can only come from reaching
OTHER, undamaged clusters that happen to also be relevant to the same query --
never from "eventually finding" the destroyed data elsewhere, which is
impossible once it's genuinely gone.
"""
import os
import sys
import json
import numpy as np
from scipy import sparse

sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from benchmarks.metrics import compute_recall
from benchmarks.containerized.evaluate import (
    rpc_client, base_arch, dummy_node_class, get_vnode_addresses,
    wait_for_ring_convergence, wait_for_finger_stability, bulk_inject,
    run_dht_query, project_root,
)


def _build_tf_matrix(courses, vocab, nfeat):
    """Sparse TF matrix, L2-normalized rows -- identical vectorization to
    node._vectorize, batched via scipy for full-corpus speed (see
    gen_queries_bigk.py, which this mirrors for the ground-truth math)."""
    rows, cols, vals = [], [], []
    for i, c in enumerate(courses):
        text = f"{c['course_title']} {c['category']} {c['description']}"
        counts = {}
        for tok in text.lower().split():
            j = vocab.get(tok)
            if j is not None:
                counts[j] = counts.get(j, 0) + 1
        for j, v in counts.items():
            rows.append(i); cols.append(j); vals.append(v)
    X = sparse.csr_matrix((np.array(vals, dtype=np.float32), (rows, cols)), shape=(len(courses), nfeat))
    norms = np.sqrt(X.multiply(X).sum(axis=1)).A.ravel()
    norms[norms == 0] = 1.0
    X = sparse.diags(1.0 / norms) @ X
    return X.tocsr()


def find_doomed_cluster_and_queries(courses, dummy_node, num_queries=15, topk=5):
    """Finds the largest K-Means cluster, then builds `num_queries` test queries
    from its own member courses (own text as query -> guaranteed top-1 match =
    this cluster), with EXACT full-corpus ground truth computed via one sparse
    cosine matmul per query (not the slow pure-python MonolithicSearcher)."""
    vocab = dummy_node.vocabulary
    nfeat = dummy_node.n_features
    centroids = np.asarray(dummy_node.centroids, dtype=np.float32)
    cnorm2 = (centroids * centroids).sum(axis=1)

    print(f"Building TF matrix for {len(courses)} courses...")
    X = _build_tf_matrix(courses, vocab, nfeat)

    print("Assigning courses to clusters...")
    cluster_of = np.empty(len(courses), dtype=np.int32)
    CH = 2000
    for s in range(0, len(courses), CH):
        chunk = X[s:s + CH].toarray().astype(np.float32)
        cluster_of[s:s + CH] = (cnorm2 - 2.0 * (chunk @ centroids.T)).argmin(axis=1)

    unique, counts = np.unique(cluster_of, return_counts=True)
    largest_idx = int(np.argmax(counts))
    doomed_cluster = int(unique[largest_idx])
    member_idx = np.where(cluster_of == doomed_cluster)[0]
    print(f"Doomed cluster: {doomed_cluster} ({len(member_idx)} courses, the largest in the corpus)")

    qidx = member_idx[:num_queries] if len(member_idx) >= num_queries else member_idx
    print(f"Building {len(qidx)} cooked queries from doomed cluster members...")

    ground_truth = []
    for qi in qidx:
        sims = (X[qi] @ X.T).toarray().ravel()
        top = np.argpartition(-sims, topk)[: topk + 1]
        top = top[np.argsort(-sims[top])][: topk]
        tc = courses[qi]
        q_text = f"{tc['course_title']} {tc['category']} {tc['description']}"
        gt_ids = [courses[j]["course_id"] for j in top]
        gt_clusters = [int(cluster_of[j]) for j in top]
        ground_truth.append({
            "query": q_text, "course": tc, "ground_truth_ids": gt_ids,
            "ground_truth_clusters": gt_clusters,
        })

    all_in_doomed = sum(1 for gt in ground_truth if all(c == doomed_cluster for c in gt["ground_truth_clusters"]))
    print(f"Of {len(ground_truth)} cooked queries, {all_in_doomed} have ALL {topk} true neighbors "
          f"inside the doomed cluster (permanently 0% recall regardless of nprobe); "
          f"{len(ground_truth) - all_in_doomed} have at least one neighbor elsewhere (recovery possible).")

    return doomed_cluster, ground_truth


def run_doomed_scenario(args, subset_courses):
    """Full pipeline: bring up the vnode ring, inject, find the doomed cluster +
    cooked queries, measure baseline (recall+hops across a wide nprobe sweep),
    kill the owner + RF successors, wait for VERIFIED healing (not a blind
    sleep -- see [[async-transport-migration]] memory on why that matters),
    measure the same sweep post-kill, save results."""
    if base_arch(args.arch) == "standard":
        print("Skipping doomed scenario for Standard DHT (nprobe logic does not apply).")
        return

    print(f"\n=== Running {args.arch} DOOMED Scenario ===")
    node_addresses = get_vnode_addresses(args)
    print(f"Ring: {len(node_addresses)} virtual nodes across {args.containers} "
          f"({args.vnodes_per_container}/container).")

    if not wait_for_ring_convergence(node_addresses, timeout=args.converge_timeout):
        return

    bulk_inject(args, node_addresses, subset_courses)
    print("Waiting for finger tables to settle before measuring...")
    wait_for_finger_stability(node_addresses, timeout=args.converge_timeout)

    HashNode = dummy_node_class(args.arch)
    dummy_node = HashNode("127.0.0.1", 5000)
    doomed_cluster, ground_truth = find_doomed_cluster_and_queries(
        subset_courses, dummy_node, num_queries=args.doomed_queries)

    client = rpc_client(node_addresses[0])
    cluster_hash = dummy_node.get_cluster_hash(doomed_cluster)
    target_node = client.find_successor(str(cluster_hash))
    print(f"Doomed cluster {doomed_cluster} (hash {cluster_hash}) is owned by: {target_node}")

    k = dummy_node.k
    default_sweep = [1, 2, 3, 5, 8, 12, 20, 40, 80, 160, 320, 640]
    requested = getattr(args, "nprobe_list", "") or ""
    if requested.strip():
        default_sweep = [int(x) for x in requested.split(",") if x.strip()]
    nprobe_values = [n for n in default_sweep if n <= max(1, k)]
    print(f"nprobe sweep: {nprobe_values}  ({len(ground_truth)} cooked queries)")

    def measure(rpc, label):
        recalls, hops_list = [], []
        for npb in nprobe_values:
            r_this, h_this = [], []
            for gt in ground_truth:
                c_json = json.dumps(gt["course"])
                try:
                    (res_tuple, hops), _ = run_dht_query(rpc, c_json, npb)
                    retrieved_ids = [json.loads(r)["course_id"] for r in res_tuple]
                    r_this.append(compute_recall(retrieved_ids, gt["ground_truth_ids"]))
                    h_this.append(hops)
                except Exception:
                    r_this.append(0.0)
            mean_r = sum(r_this) / len(r_this) if r_this else 0.0
            mean_h = sum(h_this) / len(h_this) if h_this else 0.0
            recalls.append(mean_r)
            hops_list.append(mean_h)
            print(f"  [{label}] nprobe={npb:3d} -> recall {mean_r*100:5.1f}%  hops {mean_h:6.2f}")
        return recalls, hops_list

    print("Running baseline queries (wide nprobe sweep)...")
    baseline_recalls, baseline_hops = measure(client, "baseline")

    # Same RF+1 kill mechanic as run_vnode_disaster_scenario.
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

    print(f"\nTriggering DOOMED kill: {len(to_kill)} adjacent vnodes: {to_kill}")
    for addr in to_kill:
        try:
            rpc_client(addr).stop()
        except Exception:
            pass

    surviving_nodes = [addr for addr in node_addresses if addr not in to_kill]
    if not surviving_nodes:
        print("All nodes killed; aborting.")
        return
    surviving_client = rpc_client(surviving_nodes[0])

    print("Waiting for the surviving ring to heal around the failure...")
    if not wait_for_ring_convergence(surviving_nodes, timeout=args.converge_timeout):
        print("Surviving ring did not re-converge; aborting doomed measurement.")
        return
    wait_for_finger_stability(surviving_nodes, timeout=args.converge_timeout)

    print("Running doomed queries (wide nprobe sweep, post-kill)...")
    doomed_recalls, doomed_hops = measure(surviving_client, "doomed")

    print(f"\nDoomed scenario results ({len(to_kill)} correlated kills, doomed cluster {doomed_cluster}):")
    for npb, br, bh, dr, dh in zip(nprobe_values, baseline_recalls, baseline_hops, doomed_recalls, doomed_hops):
        print(f"  nprobe={npb:3d} -> baseline {br*100:5.1f}% ({bh:5.2f} hops)  "
              f"doomed {dr*100:5.1f}% ({dh:5.2f} hops)  delta {100*(dr-br):+5.1f}pp")

    results = {
        "arch": args.arch,
        "num_nodes": len(node_addresses),
        "doomed_cluster": doomed_cluster,
        "num_cooked_queries": len(ground_truth),
        "correlated_kills": len(to_kill),
        "surviving_nodes": len(surviving_nodes),
        "nprobe_values": nprobe_values,
        "baseline_recall_by_nprobe": baseline_recalls,
        "baseline_hops_by_nprobe": baseline_hops,
        "doomed_recall_by_nprobe": doomed_recalls,
        "doomed_hops_by_nprobe": doomed_hops,
    }
    out_path = os.path.join(project_root(), "data", "benchmarks", "results", "containerized",
                             f"doomed_scenario_results_{args.arch}.json")
    with open(out_path, "w") as f:
        json.dump(results, f, indent=4)
    print(f"Doomed scenario results saved to {out_path}")
