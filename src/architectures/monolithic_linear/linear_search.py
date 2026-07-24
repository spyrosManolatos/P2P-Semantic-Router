import json
import sys
import os

import numpy as np

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from core.config_loader import load_config

def load_vocab(path):
    if not os.path.exists(path):
        return {}
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f).get('vocabulary', {})

def vectorize(text, vocab):
    words = text.lower().split()
    vec = [0.0] * len(vocab)
    for w in words:
        if w in vocab:
            vec[vocab[w]] += 1.0
    norm = sum(v * v for v in vec) ** 0.5
    if norm > 0:
        return [v / norm for v in vec]
    return vec

class MonolithicSearcher:
    """Exact cosine-similarity ground truth. Course vectors are built ONCE as a
    dense numpy matrix at construction (chunked, same approach as
    evaluate.bulk_load_direct), so search() is a single matvec + top-k instead
    of re-vectorizing every course from scratch in pure Python on every call --
    at full-corpus scale (98k courses) the naive per-query pure-Python rescan
    made even a 50-query ground-truth pass take on the order of hours."""

    def __init__(self, dataset="kaggle", limit=None):
        config = load_config()
        project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        if dataset == "synthetic":
            data_path = os.path.join(project_root, config['storage']['data']['synthetic_path'])
            centroids_path = os.path.join(project_root, config['storage']['centroids']['synthetic_path'])
        else:
            data_path = os.path.join(project_root, config['storage']['data']['kaggle']['normalized_path'])
            centroids_path = os.path.join(project_root, config['storage']['centroids']['kaggle_dataset_path'])

        if not os.path.exists(data_path):
            data_path = config['storage']['data']['synthetic_path']
            centroids_path = config['storage']['centroids']['synthetic_path']

        with open(data_path, 'r', encoding='utf-8') as f:
            self.courses = json.load(f)

        if limit is not None:
            self.courses = self.courses[:limit]

        self.vocab = load_vocab(centroids_path)
        print(f"[Monolithic Baseline] Loaded {len(self.courses)} courses from: {data_path}")

        self._build_matrix()

    def _build_matrix(self):
        nfeat = len(self.vocab)
        n = len(self.courses)
        self._matrix = np.zeros((n, nfeat), dtype=np.float32)
        CH = 2000
        for s in range(0, n, CH):
            chunk = self.courses[s:s + CH]
            X = np.zeros((len(chunk), nfeat), dtype=np.float32)
            for r, c in enumerate(chunk):
                text = f"{c['course_title']} {c['category']} {c['description']}"
                for tok in text.lower().split():
                    j = self.vocab.get(tok)
                    if j is not None:
                        X[r, j] += 1.0
            norms = np.sqrt((X * X).sum(axis=1))
            norms[norms == 0] = 1.0
            X /= norms[:, None]
            self._matrix[s:s + CH] = X

    def search(self, query_text: str, top_k: int = 5):
        q_vec = np.asarray(vectorize(query_text, self.vocab), dtype=np.float32)
        sims = self._matrix @ q_vec
        n = len(sims)
        k = min(top_k, n)
        top_idx = np.argpartition(-sims, k - 1)[:k]
        top_idx = top_idx[np.argsort(-sims[top_idx])]
        return [(float(sims[i]), self.courses[i]) for i in top_idx]

def run():
    print("=== Monolithic Linear Similarity Search (Ground Truth) ===")
    searcher = MonolithicSearcher(dataset="kaggle")

    # Target Query Course
    query_course = searcher.courses[0]
    query_text = f"{query_course['course_title']} {query_course['category']} {query_course['description']}"

    print(f"Querying for courses similar to: '{query_course['course_title']}'")

    results = searcher.search(query_text, top_k=5)

    print("\nTop 5 Results (Exact Centralized Linear Search):")
    for idx, (sim, c) in enumerate(results):
        print(f"  {idx + 1}. {c['course_title']} (ID: {c['course_id']}) [Similarity: {sim:.4f}]")
    print("-" * 50)

if __name__ == "__main__":
    run()
