"""
Fast full-corpus query/ground-truth generator for the scaling experiment.

Same output schema as evaluate.py --gen-queries (queries.json), but:
- computes exact top-5 ground truth over the FULL corpus with numpy/scipy
  (the pure-python MonolithicSearcher would need hours at 98k docs);
- vectorization replicates node._vectorize EXACTLY (text.lower().split(),
  count words in the artifact vocabulary, L2-normalize), so the ground truth
  lives in the same vector space the DHT nodes rank in;
- writes to a SEPARATE file in data/benchmarks/queries/ so the fixed 500-scale
  query set (queries_500.json, used by every scale/fault/join/disaster/load
  experiment) is untouched.

Usage:
  python -m src.benchmarks.containerized.gen_queries_bigk \
      --centroids data/models/kaggle_centroids_k5500.json \
      --data data/raw/normalized_kaggle_courses.json \
      --out data/benchmarks/queries/queries_98k_k4096.json --queries 50
"""
import os
import json
import argparse
import random

import numpy as np
from scipy import sparse


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--centroids", type=str, default="data/models/kaggle_centroids_k5500.json")
    p.add_argument("--data", type=str, default="data/raw/normalized_kaggle_courses.json")
    p.add_argument("--out", type=str, default="data/benchmarks/queries/queries_98k_k4096.json")
    p.add_argument("--queries", type=int, default=50)
    p.add_argument("--topk", type=int, default=5)
    args = p.parse_args()

    with open(args.centroids) as f:
        art = json.load(f)
    vocab = art["vocabulary"]
    centroids = np.asarray(art["centroids"], dtype=np.float32)  # (k, nfeat)
    nfeat = max(vocab.values()) + 1
    with open(args.data) as f:
        courses = json.load(f)
    n = len(courses)
    print(f"{n} courses, vocab={len(vocab)} terms ({nfeat} dims), k={len(centroids)} clusters")

    # TF vectors, exactly as node._vectorize builds them.
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
    X = sparse.csr_matrix((np.array(vals, dtype=np.float32), (rows, cols)), shape=(n, nfeat))
    # L2 normalize rows -> dot product = cosine (same as the nodes)
    norms = np.sqrt(X.multiply(X).sum(axis=1)).A.ravel()
    norms[norms == 0] = 1.0
    X = sparse.diags(1.0 / norms) @ X
    X = X.tocsr()
    print("TF matrix built.")

    # Assign every course to its nearest centroid (the same argmin the router
    # uses), so each query can be tagged with the cluster it "comes from".
    # Chunked to bound memory on large corpora (extensible to krylov-scale runs).
    cnorm2 = (centroids * centroids).sum(axis=1)
    cluster_of = np.empty(n, dtype=np.int32)
    CH = 2000
    for s in range(0, n, CH):
        chunk = X[s:s + CH].toarray().astype(np.float32)
        cluster_of[s:s + CH] = (cnorm2 - 2.0 * (chunk @ centroids.T)).argmin(axis=1)
    print("Cluster assignment done.")

    random.seed(42)
    qidx = random.sample(range(n), min(args.queries, n))

    ground_truth = []
    for qi in qidx:
        sims = (X[qi] @ X.T).toarray().ravel()
        top = np.argpartition(-sims, args.topk)[: args.topk + 1]
        top = top[np.argsort(-sims[top])][: args.topk]  # exact top-k, self included
        tc = courses[qi]
        q_text = f"{tc['course_title']} {tc['category']} {tc['description']}"
        gt_ids = [courses[j]["course_id"] for j in top]
        ground_truth.append({
            "query": q_text, "course": tc, "ground_truth_ids": gt_ids,
            "query_cluster": int(cluster_of[qi]),
            "ground_truth_clusters": [int(cluster_of[j]) for j in top],
        })
        print(f"  [{tc['course_id']}] cl={int(cluster_of[qi])} "
              f"{tc['course_title'][:50]} -> {gt_ids}")

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({"dataset": "kaggle", "dataset_size": n, "ground_truth": ground_truth}, f, indent=4)
    print(f"\nSaved {len(ground_truth)} queries with full-corpus GT to {args.out}")


if __name__ == "__main__":
    main()
