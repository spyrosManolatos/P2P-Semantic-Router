import os
import json
import argparse
from typing import List, Dict, Any
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
from scipy.cluster.hierarchy import linkage, leaves_list
import sys

# Ensure src/ is in the python path to import config_loader
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import config_loader

def main():
    config = config_loader.load_config()
    
    parser = argparse.ArgumentParser(description="Train KMeans centroids on synthetic course data using TF-IDF.")
    parser.add_argument("--k", type=int, default=config['kmeans']['clusters'], help="Number of clusters (centroids) to generate.")
    args = parser.parse_args()

    data_path = config['storage']['data_path']
    if not os.path.exists(data_path):
        print(f"Error: {data_path} not found. Please generate synthetic data first.")
        return

    # 1. Load data
    with open(data_path, "r", encoding="utf-8") as f:
        courses: List[Dict[str, Any]] = json.load(f)
    print(f"Loaded {len(courses)} courses from {data_path}.")

    if len(courses) < args.k:
        print(f"Error: Cannot generate {args.k} clusters from only {len(courses)} courses.")
        return

    # 2. Extract text for vectorization
    # We combine title, category, and description to form the semantic representation of the course.
    corpus = []
    for c in courses:
        text = f"{c['course_title']} {c['category']} {c['description']}"
        corpus.append(text)

    # 3. TF-IDF Vectorization
    print("Vectorizing text using TF-IDF...")
    vectorizer = TfidfVectorizer(stop_words='english', max_features=1000)
    X = vectorizer.fit_transform(corpus)
    print(f"Generated {X.shape[1]} features (dimensions) for {X.shape[0]} courses.")

    # 4. K-Means Clustering
    print(f"Running K-Means clustering to find {args.k} global centroids...")
    kmeans = KMeans(n_clusters=args.k, random_state=42, n_init=10)
    kmeans.fit(X)
    
    raw_centroids = kmeans.cluster_centers_
    raw_labels = kmeans.labels_

    # 4.5 Agglomerative 1D Ordering (Dendrogram Hierarchy)
    # We build a hierarchical tree of the 5 centroids and flatten it to get a 1D left-to-right order.
    # This guarantees that adjacent Cluster IDs (0, 1, 2...) are semantically similar!
    print("Running Agglomerative Clustering on centroids to establish 1D Semantic Ring Mapping...")
    Z = linkage(raw_centroids, method='average', metric='euclidean')
    ordered_indices = leaves_list(Z)
    
    # Re-order the centroids
    centroids = raw_centroids[ordered_indices]
    
    # Create a mapping from old K-Means label to new Dendrogram label
    label_map = {old_idx: new_idx for new_idx, old_idx in enumerate(ordered_indices)}
    labels = [label_map[l] for l in raw_labels]

    # Create a mapping of cluster_id -> list of courses that fall into it
    cluster_map = {i: [] for i in range(args.k)}
    for i, label in enumerate(labels):
        cluster_map[label].append(courses[i])

    # Print debugging distribution
    print("\n" + "="*50)
    print("CLUSTER DISTRIBUTION RESULTS")
    print("="*50)
    for cluster_id in range(args.k):
        c_list = cluster_map[cluster_id]
        print(f"\nCluster {cluster_id}: {len(c_list)} courses")
        # Print a few examples to verify semantic closeness
        sample_size = min(3, len(c_list))
        print(f"  Examples:")
        for j in range(sample_size):
            print(f"    - [{c_list[j]['category']}] {c_list[j]['course_title']}")

    # 6. Save centroids and vectorizer vocabulary
    # Note: For the actual DHT routing later, nodes will need the vectorizer vocabulary 
    # to transform new queries into the same vector space, and the centroids to calculate distances.
    out_data = {
        "k": args.k,
        "n_features": X.shape[1],
        "vocabulary": {k: int(v) for k, v in vectorizer.vocabulary_.items()},
        "centroids": centroids.tolist()  # Convert numpy array to list for JSON serialization
    }

    out_path = config['storage']['centroids_path']
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(out_data, f, indent=4)
    
    print("\n" + "="*50)
    print(f"Successfully saved {args.k} centroids to {out_path}.")
    print("="*50)

if __name__ == "__main__":
    main()
