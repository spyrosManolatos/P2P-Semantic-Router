import json
import random
import time
from utils import (
    setup_network, 
    load_courses, 
    inject_courses, 
    print_separator, 
    print_storage_summary, 
    teardown_network
)

def main():
    nodes = setup_network()
    courses = load_courses()
    
    # Prove the Ring Mapping
    print_separator()
    print("Chord Ring Topology & Non-Random Cluster Routing Spaces:")
    print_separator()
    
    cluster_destinations = {}
    for cid in range(5):
        c_hash = nodes[0].get_cluster_hash(cid)
        dest_node = nodes[0].find_successor(str(c_hash))
        cluster_destinations[cid] = (c_hash, dest_node)

    for node in nodes:
        info = node.get_info()
        print(f"Address: {info['address']}")
        print(f"  Node ID:     {info['node_id']}")
        print(f"  Predecessor: {info['predecessor']}")
        print(f"  Successor:   {info['successor']}")
        
        mapped_clusters = [cid for cid, (_, dest) in cluster_destinations.items() if dest == node.address]
        print(f"  Responsible for mathematical clusters: {mapped_clusters}")
        print("-" * 40)

    # Inject data
    inject_courses(nodes, courses)
    
    # Test Semantic KNN
    print_separator()
    print("Testing Distributed Semantic Similarity Search (KNN)...")
    print_separator()
    
    query_course = random.choice(courses)
    query_json = json.dumps(query_course)
    print(f"Query Course: '{query_course['course_title']}' [{query_course['category']}]")
    
    query_node = random.choice(nodes)
    
    print("\n--- TEST 1: Fast Search (nprobe=1) ---")
    start_time = time.time()
    results_n1 = query_node.get_similar_courses(query_json, nprobe=1)
    dur_n1 = (time.time() - start_time) * 1000
    print(f"nprobe=1 returned {len(results_n1)} courses in {dur_n1:.1f}ms")
    
    print("\n--- TEST 2: High Recall Search (nprobe=2 Fanout) ---")
    start_time = time.time()
    results_n2 = query_node.get_similar_courses(query_json, nprobe=2)
    dur_n2 = (time.time() - start_time) * 1000
    print(f"nprobe=2 returned {len(results_n2)} courses in {dur_n2:.1f}ms")

    print_storage_summary(nodes)
    
    teardown_network(nodes)

if __name__ == "__main__":
    main()
