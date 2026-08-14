"""
Fine-grained centroid training for the full-corpus scaling experiment.

Separate from train_centroids.py (which stays the K=80 production trainer):
- MiniBatchKMeans instead of full KMeans -- at k=4096 on ~98k documents the
  full Lloyd's algorithm is impractical; MiniBatch is the standard choice at
  this granularity.
- Writes to an EXPLICIT --out path instead of the config-driven artifact, so
  the production kaggle_centroids.json (K=80, used by every existing result)
  is never touched.

Pipeline is otherwise identical: TF-IDF (max_features=1000, english stop
words) -> KMeans -> agglomerative leaf-ordering of the centroids (linkage +
leaves_list), so adjacent cluster IDs remain semantically similar -- the
property the semantic ring mapping depends on. Output schema matches
train_centroids.py exactly: {k, n_features, vocabulary, centroids}.

Usage:
  python src/ml/train_centroids_bigk.py --k 4096 \
      --data data/raw/normalized_kaggle_courses.json \
      --out  data/models/kaggle_centroids_k5500.json
"""
import os
import json
import argparse

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import MiniBatchKMeans
import time
from scipy.cluster.hierarchy import linkage, leaves_list, optimal_leaf_ordering


def main():
    parser = argparse.ArgumentParser(description="Train fine-grained MiniBatchKMeans centroids (big-k).")
    parser.add_argument("--k", type=int, default=4096)
    parser.add_argument("--data", type=str, default="data/raw/normalized_kaggle_courses.json")
    parser.add_argument("--out", type=str, default="data/models/kaggle_centroids_k5500.json")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    with open(args.data, "r", encoding="utf-8") as f:
        courses = json.load(f)
    print(f"Loaded {len(courses)} courses from {args.data}.")
    if len(courses) < args.k:
        raise SystemExit(f"Cannot make {args.k} clusters from {len(courses)} courses.")

    corpus = [f"{c['course_title']} {c['category']} {c['description']}" for c in courses]

    print("TF-IDF vectorization (max_features=1000)...")
    vectorizer = TfidfVectorizer(stop_words="english", max_features=1000)
    X = vectorizer.fit_transform(corpus)
    print(f"  {X.shape[0]} docs x {X.shape[1]} features")

    print(f"MiniBatchKMeans(k={args.k})... (the slow part)")
    km = MiniBatchKMeans(n_clusters=args.k, random_state=args.seed,
                         batch_size=8192, n_init=3, max_iter=100)
    km.fit(X)

    # Agglomerative 1-D leaf ordering: relabel clusters so ADJACENT IDs are
    # semantically similar -- the property the semantic ring mapping depends on
    # for recall (contiguous-ID nprobe expansion only covers a query's true
    # neighbours if those neighbours have nearby IDs).
    #
    # optimal_leaf_ordering re-sequences the dendrogram leaves to MINIMISE the
    # sum of distances between adjacent leaves (Bar-Joseph et al.), which is a
    # strictly tighter 1-D embedding than the arbitrary orientation leaves_list
    # returns from linkage alone. Plain leaves_list left many clusters' true
    # nearest neighbours hundreds of IDs away (measured: cluster 5495's
    # neighbours at ID-gaps up to ~1470), capping recall; this pulls them in.
    # It is O(n^2)-O(n^3) in the leaf count, so it is the slow step at k=5500.
    print("Optimal leaf-ordering centroids (Bar-Joseph, minimises adjacent distance)...")
    t0 = time.time()
    Z = linkage(km.cluster_centers_, method="average", metric="euclidean")
    Z = optimal_leaf_ordering(Z, km.cluster_centers_, metric="euclidean")
    order = leaves_list(Z)
    centroids = km.cluster_centers_[order]
    print(f"  ordering done in {time.time() - t0:.1f}s")

    out_data = {
        "k": args.k,
        "n_features": X.shape[1],
        "vocabulary": {w: int(i) for w, i in vectorizer.vocabulary_.items()},
        # round to 6 decimals: shrinks the JSON ~2x with no measurable effect on
        # nearest-centroid assignment (TF-IDF components are O(0.1))
        "centroids": [[round(float(v), 6) for v in row] for row in centroids],
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out_data, f)
    size_mb = os.path.getsize(args.out) / 1e6
    print(f"Saved k={args.k} artifact to {args.out} ({size_mb:.1f} MB).")


if __name__ == "__main__":
    main()
