import json
import time
import sys
import os

from utils import teardown_network, load_courses
from core.config_loader import load_config
from node import NaiveChordNode

def run():
    print("=== Simulation 03: Naive Load Balancing vs Flooding ===")
    config = load_config()
    num_nodes = 3
    base_port = 9000
    nodes = []
    
    try:
        boot_node = NaiveChordNode("127.0.0.1", base_port)
        nodes.append(boot_node)
        for i in range(1, num_nodes):
            port = base_port + i
            node = NaiveChordNode("127.0.0.1", port, bootstrap_node=f"127.0.0.1:{base_port}")
            nodes.append(node)
            time.sleep(1)
            
        print("Waiting 5 seconds for ring stabilization...")
        time.sleep(5)
        
        courses = load_courses()
        for c in courses[:20]:
            boot_node.put_course(json.dumps(c))
            
        print("-" * 50)
        query_course = courses[0]
        print(f"Simulating a traffic spike: 5 identical concurrent queries...")
        
        # We fire off queries sequentially, but fast, to see the printouts
        start_time = time.time()
        for i in range(5):
            print(f"\\n--- Query {i+1} ---")
            nodes[2].get_similar_courses(json.dumps(query_course))
            
        end_time = time.time()
        print("-" * 50)
        print(f"Total time for 5 queries: {end_time - start_time:.4f} seconds")
        print("Notice that EVERY query forces the crawler to traverse the ENTIRE ring (O(N) Overhead).")
        print("Unlike Semantic DHT, Naive DHT has no CPU Delegation based on Cluster Popularity, it just blindly floods the network every time.")
        
    finally:
        teardown_network(nodes)

if __name__ == "__main__":
    run()
