import json
import math
import sys
import os

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from src.config_loader import load_config

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

def run():
    print("=== Monolithic Linear Similarity Search (Ground Truth) ===")
    config = load_config()
    
    # Path resolution
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    data_path = os.path.join(project_root, config['storage']['data_path'])
    centroids_path = os.path.join(project_root, config['storage']['centroids_path'])
    
    if not os.path.exists(data_path):
        data_path = config['storage']['data_path']
        centroids_path = config['storage']['centroids_path']

    with open(data_path, 'r', encoding='utf-8') as f:
        courses = json.load(f)
        
    vocab = load_vocab(centroids_path)
    print(f"Loaded {len(courses)} courses from: {data_path}")
    print(f"Loaded vocabulary size: {len(vocab)}")
    print("-" * 50)
    
    # Target Query Course
    query_course = courses[0] # "Methods of Artificial Intelligence"
    print(f"Querying for courses similar to: '{query_course['course_title']}'")
    
    # Vectorize query
    q_text = f"{query_course['course_title']} {query_course['category']} {query_course['description']}"
    q_vec = vectorize(q_text, vocab)
    
    scored = []
    for c in courses:
        c_text = f"{c['course_title']} {c['category']} {c['description']}"
        c_vec = vectorize(c_text, vocab)
        sim = sum(qv * cv for qv, cv in zip(q_vec, c_vec))
        scored.append((sim, c))
        
    # Sort by Cosine Similarity descending
    scored.sort(key=lambda x: x[0], reverse=True)
    
    print("\nTop 5 Results (Exact Centralized Linear Search):")
    for idx, (sim, c) in enumerate(scored[:5]):
        print(f"  {idx + 1}. {c['course_title']} (ID: {c['course_id']}) [Similarity: {sim:.4f}]")
    print("-" * 50)

if __name__ == "__main__":
    run()
