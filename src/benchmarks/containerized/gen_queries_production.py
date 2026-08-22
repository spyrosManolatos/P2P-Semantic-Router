"""
Production-shaped query/ground-truth generator.

`gen_queries_bigk.py` samples query source documents UNIFORMLY over the corpus.
Because cluster sizes are themselves skewed (mean 17.8, median 3, max 1619 at
k=5500), that already yields query traffic proportional to topic size -- but
real search traffic is far more concentrated than content is. Query frequency
in production follows Zipf's law over topics: a small head of popular subjects
absorbs most of the volume, with a very long tail of rare ones.

That difference matters for the correlated-failure experiments specifically.
Uniform-over-documents traffic spreads queries thinly across 400 of 5500
clusters, so killing any single node touches few of them. Production traffic
concentrates on exactly the big, popular clusters that a load-aware placement
piles onto the same peers -- so the hot node holds both the most data AND the
most-queried data, and losing it is proportionally worse. Measuring resilience
under uniform traffic therefore reports a best case that no deployment sees.

Sampling model:
  1. rank non-empty clusters by size (biggest = most popular topic);
  2. draw a cluster with P(rank r) proportional to 1/r^s  (s=1.0 = classic Zipf);
  3. draw a source document uniformly WITHIN that cluster.
Distinct documents are used so every query is a real, separate retrieval task;
the Zipf weighting shows up as many queries landing in the same hot clusters,
which is the property the experiments care about.

Ground truth is the exact top-k over the FULL corpus in the same TF vector
space the nodes rank in (identical vectorization to node._vectorize), so the
output is schema-compatible with the existing query files and drops straight
into evaluate.py --queries_file.

Usage:
  python -m src.benchmarks.containerized.gen_queries_production \
      --centroids data/models/kaggle_centroids_k5500.json \
      --data data/raw/normalized_kaggle_courses.json \
      --out data/benchmarks/queries/queries_98k_production_500.json \
      --queries 500 --zipf 1.0
"""
import os
import json
import argparse
import random
from collections import defaultdict

import numpy as np
from scipy import sparse


def build_tf(courses, vocab, nfeat):
    """TF vectors exactly as node._vectorize builds them: lower().split(),
    count terms present in the artifact vocabulary, L2-normalize so a dot
    product is the cosine the nodes rank by."""
    rows, cols, vals = [], [], []
    for i, c in enumerate(courses):
        text = f"{c['course_title']} {c['category']} {c['description']}"
        counts = {}
        for tok in text.lower().split():
            j = vocab.get(tok)
            if j is not None:
                counts[j] = counts.get(j, 0) + 1
        for j, v in counts.items():
            rows.append(i)
            cols.append(j)
            vals.append(v)
    X = sparse.csr_matrix((np.array(vals, dtype=np.float32), (rows, cols)),
                          shape=(len(courses), nfeat))
    norms = np.sqrt(X.multiply(X).sum(axis=1)).A.ravel()
    norms[norms == 0] = 1.0
    return (sparse.diags(1.0 / norms) @ X).tocsr()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--centroids", default="data/models/kaggle_centroids_k5500.json")
    p.add_argument("--data", default="data/raw/normalized_kaggle_courses.json")
    p.add_argument("--out", default="data/benchmarks/queries/queries_98k_production_500.json")
    p.add_argument("--queries", type=int, default=500)
    p.add_argument("--topk", type=int, default=5)
    p.add_argument("--zipf", type=float, default=1.0,
                   help="Zipf exponent s over size-ranked clusters. 1.0 is the "
                        "classic search-log value; 0 would reduce to picking "
                        "topics uniformly, ignoring popularity entirely.")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    with open(args.centroids) as f:
        art = json.load(f)
    vocab = art["vocabulary"]
    centroids = np.asarray(art["centroids"], dtype=np.float32)
    nfeat = max(vocab.values()) + 1
    with open(args.data) as f:
        courses = json.load(f)
    n = len(courses)
    print(f"{n} courses, vocab={len(vocab)} ({nfeat} dims), k={len(centroids)}")

    X = build_tf(courses, vocab, nfeat)
    print("TF matrix built.")

    cnorm2 = (centroids * centroids).sum(axis=1)
    cluster_of = np.empty(n, dtype=np.int32)
    CH = 2000
    for s in range(0, n, CH):
        chunk = X[s:s + CH].toarray().astype(np.float32)
        cluster_of[s:s + CH] = (cnorm2 - 2.0 * (chunk @ centroids.T)).argmin(axis=1)
    print("Cluster assignment done.")

    members = defaultdict(list)
    for i, c in enumerate(cluster_of):
        members[int(c)].append(i)
    # Rank by size: rank 1 is the biggest cluster, i.e. the most popular topic.
    ranked = sorted(members.keys(), key=lambda c: -len(members[c]))
    weights = np.array([1.0 / (r + 1) ** args.zipf for r in range(len(ranked))])
    weights /= weights.sum()
    print(f"{len(ranked)} non-empty clusters; biggest is {ranked[0]} "
          f"with {len(members[ranked[0]])} docs "
          f"({100*len(members[ranked[0]])/n:.2f}% of corpus)")

    rng = random.Random(args.seed)
    nprng = np.random.default_rng(args.seed)
    picked, used = [], set()
    attempts = 0
    while len(picked) < min(args.queries, n) and attempts < args.queries * 200:
        attempts += 1
        cid = ranked[int(nprng.choice(len(ranked), p=weights))]
        pool = [i for i in members[cid] if i not in used]
        if not pool:
            continue  # hot cluster exhausted; redraw
        qi = rng.choice(pool)
        used.add(qi)
        picked.append(qi)

    per_cluster = defaultdict(int)
    for qi in picked:
        per_cluster[int(cluster_of[qi])] += 1
    top = sorted(per_cluster.items(), key=lambda kv: -kv[1])[:8]
    print(f"\nsampled {len(picked)} queries over {len(per_cluster)} distinct clusters")
    print("  busiest targeted clusters (cluster -> queries):")
    for cid, cnt in top:
        print(f"    {cid:>5} -> {cnt:>3} queries   ({len(members[cid])} docs in cluster)")

    ground_truth = []
    for num, qi in enumerate(picked, 1):
        sims = (X[qi] @ X.T).toarray().ravel()
        cand = np.argpartition(-sims, args.topk)[: args.topk + 1]
        cand = cand[np.argsort(-sims[cand])][: args.topk]
        tc = courses[qi]
        ground_truth.append({
            "query": f"{tc['course_title']} {tc['category']} {tc['description']}",
            "course": tc,
            "ground_truth_ids": [courses[j]["course_id"] for j in cand],
            "query_cluster": int(cluster_of[qi]),
            "ground_truth_clusters": [int(cluster_of[j]) for j in cand],
        })
        if num % 100 == 0:
            print(f"  ground truth {num}/{len(picked)}")

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"dataset": "kaggle", "dataset_size": n,
                   "traffic_model": f"zipf(s={args.zipf}) over size-ranked clusters",
                   "ground_truth": ground_truth}, f, indent=4)
    print(f"\nSaved {len(ground_truth)} production-shaped queries to {args.out}")


if __name__ == "__main__":
    main()
