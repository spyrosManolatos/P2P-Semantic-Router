import json
import math

class SemanticTester:
    def __init__(self):
        with open("data/models/centroids.json", "r") as f:
            data = json.load(f)
            self.k = data["k"]
            self.n_features = data["n_features"]
            self.vocabulary = data["vocabulary"]
            self.centroids = data["centroids"]
            
    def vectorize(self, text):
        tokens = text.lower().split()
        vec = [0.0] * self.n_features
        for token in tokens:
            if token in self.vocabulary:
                idx = self.vocabulary[token]
                vec[idx] += 1.0
        norm = math.sqrt(sum(v*v for v in vec))
        if norm > 0:
            vec = [v/norm for v in vec]
        return vec

    def find_clusters(self, text, nprobe):
        vec = self.vectorize(text)
        similarities = []
        for i, c_vec in enumerate(self.centroids):
            sim = sum(v * c for v, c in zip(vec, c_vec))
            similarities.append((i, sim))
        similarities.sort(key=lambda x: x[1], reverse=True)
        return [sim[0] for sim in similarities[:nprobe]]

tester = SemanticTester()
queries = [
    "machine learning and artificial intelligence",
    "web development javascript react",
    "data science python pandas",
    "business management finance",
    "graphic design photoshop illustrator"
]
print("Semantic Router Clusters for nprobe=2:")
for q in queries:
    clusters = tester.find_clusters(q, 2)
    print(f"'{q}' -> {clusters}")
