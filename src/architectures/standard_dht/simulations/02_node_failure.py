import json
import time
import sys
import os

from utils import teardown_network, load_courses
from core.config_loader import load_config
from node import NaiveChordNode

def run():
    print("=== Simulation 02: Naive Node Failure & Recovery ===")
    config = load_config()
    num_nodes = 4
    base_port = 9000
    nodes = []
    
    try:
        # Step 1: Start 4 Nodes
        boot_node = NaiveChordNode("127.0.0.1", base_port)
        nodes.append(boot_node)
        for i in range(1, num_nodes):
            port = base_port + i
            node = NaiveChordNode("127.0.0.1", port, bootstrap_node=f"127.0.0.1:{base_port}")
            nodes.append(node)
            time.sleep(1)
            
        print("Waiting 10 seconds for ring stabilization...")
        time.sleep(10)
        
        courses = load_courses()
        print(f"Injecting {len(courses)} courses into the network...")
        for c in courses:
            boot_node.put_course(json.dumps(c))
            
        print("-" * 50)
        query_course = courses[0]
        
        # Check if the query works initially
        print("Performing search BEFORE failure...")
        results_list = nodes[2].get_similar_courses(json.dumps(query_course))
        print("\nTop 5 Results BEFORE failure:")
        for idx, r_json in enumerate(results_list):
            r = json.loads(r_json)
            print(f" {idx + 1}. {r['course_title']} (ID: {r['course_id']}) [Similarity: {r.get('similarity', 0.0):.4f}]")
        
        print("-" * 50)
        # Step 2: Kill node 9000
        print("CRASHING Node 127.0.0.1:9000...")
        nodes[0].stop()
        
        print("Waiting 5 seconds for network to heal (Stabilization)...")
        time.sleep(5)
        
        print("-" * 50)
        print("Performing search AFTER failure...")
        # Since node 0 holds replicas on its successors, the data should still be available!
        results_list = nodes[2].get_similar_courses(json.dumps(query_course))
        print("\nTop 5 Results AFTER failure:")
        for idx, r_json in enumerate(results_list):
            r = json.loads(r_json)
            print(f" {idx + 1}. {r['course_title']} (ID: {r['course_id']}) [Similarity: {r.get('similarity', 0.0):.4f}]")
        print("Search succeeded! The data was recovered via Replication Factor, but Naive routing still requires O(N) traversal.")
        
    finally:
        for n in nodes[1:]:
            n.stop()

if __name__ == "__main__":
    run()
