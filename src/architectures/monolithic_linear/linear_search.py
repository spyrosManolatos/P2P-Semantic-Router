import json
import math
import sys
import os

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
    norm = math.sqrt(sum(v*v for v in vec))
    if norm > 0:
        return [v/norm for v in vec]
    return vec

class MonolithicSearcher:
    def __init__(self, dataset="kaggle"):
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
            
        self.vocab = load_vocab(centroids_path)
        print(f"[Monolithic Baseline] Loaded {len(self.courses)} courses from: {data_path}")
        
    def search(self, query_text: str, top_k: int = 5):
        q_vec = vectorize(query_text, self.vocab)
        
        scored = []
        for c in self.courses:
            c_text = f"{c['course_title']} {c['category']} {c['description']}"
            c_vec = vectorize(c_text, self.vocab)
            sim = sum(qv * cv for qv, cv in zip(q_vec, c_vec))
            scored.append((sim, c))
            
        # Sort by Cosine Similarity descending
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

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
