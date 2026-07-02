import os
import sys
import json
import time
import random

# Add the parent directory to sys.path so we can import ChordNode
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from node import ChordNode

def print_separator():
    print("=" * 70)

def print_storage_summary(nodes, title="Distributed Storage Summary (Semantic Clusters):"):
    print_separator()
    print(title)
    print_separator()
    for node in nodes:
        info = node.get_info()
        print(f"Node {info['address']}:")
        if info['primary_summary']:
            print("  [PRIMARY DATA]")
            for cluster_id, count in info['primary_summary'].items():
                print(f"    - Cluster '{cluster_id}': {count} courses")
        else:
            print("  [PRIMARY DATA] None")
            
        if info['replica_summary']:
            print("  [REPLICA DATA]")
            for cluster_id, count in info['replica_summary'].items():
                print(f"    - Cluster '{cluster_id}': {count} courses")
        else:
            print("  [REPLICA DATA] None")
        print("-" * 40)

def load_courses():
    synth_data_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "synth_data", "data.json")
    with open(synth_data_path, "r", encoding="utf-8") as f:
        courses = json.load(f)
    print(f"Loaded {len(courses)} courses from {synth_data_path}")
    return courses

def setup_network(num_nodes=4, base_port=8001):
    print_separator()
    print(f"Starting Semantic DHT Peer Simulation with {num_nodes} Nodes...")
    print_separator()

    base_ip = "127.0.0.1"
    nodes = []

    for i in range(num_nodes):
        node = ChordNode(base_ip, base_port + i)
        node.start()
        nodes.append(node)
    
    print("\nNodes initialized. Forming Chord Ring...")
    nodes[0].join(None)
    
    bootstrap_addr = nodes[0].address
    for node in nodes[1:]:
        time.sleep(0.5)
        node.join(bootstrap_addr)

    print("\nWaiting for Chord ring stabilization (5 seconds)...")
    time.sleep(5.0)
    
    return nodes

def inject_courses(nodes, courses):
    print_separator()
    print("Inserting (PUT) courses into DHT (using semantic vectorization & centroid routing)...")
    print_separator()
    for course in courses:
        course_json = json.dumps(course)
        entry_node = random.choice(nodes)
        entry_node.put_course(course_json)
    print(f"Successfully finished routing all {len(courses)} courses.")
    time.sleep(2.0)  # Allow writes/replication to settle

def teardown_network(nodes):
    print_separator()
    print("Stopping all nodes...")
    print_separator()
    for node in nodes:
        node.stop()
    print("\nSimulation completed successfully.")
