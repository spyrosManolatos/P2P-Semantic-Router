import os
import sys
import json
import time
import random

# Add the root 'src/' directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
# Add the architecture directory so we can import 'node'
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from architectures.semantic_router.node import ChordNode
from core import config_loader

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

def load_courses(dataset="kaggle"):
    config = config_loader.load_config()
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if dataset == "synthetic":
        data_path = os.path.join(project_root, config['storage']['data']['synthetic_path'])
    else:
        data_path = os.path.join(project_root, config['storage']['data']['kaggle']['normalized_path'])
        
    if not os.path.exists(data_path):
        data_path = config['storage']['data']['synthetic_path']
        
    with open(data_path, "r", encoding="utf-8") as f:
        courses = json.load(f)
    print(f"Loaded {len(courses)} courses from {data_path}")
    return courses
def setup_network(num_nodes=None, base_port=None, r=None, dataset="kaggle"):
    config = config_loader.load_config()
    num_nodes = num_nodes if num_nodes is not None else config['network']['number_of_nodes']
    base_port = base_port if base_port is not None else config['network']['default_port']
    r = r if r is not None else config['dht']['replication_factor']
    
    print_separator()
    print(f"Starting Semantic DHT Peer Simulation with {num_nodes} Nodes (RF={r})...")
    print_separator()

    base_ip = config['network']['default_host']
    nodes = []

    # 1. Spawn and start the Bootstrap Node
    bootstrap_node = ChordNode(base_ip, base_port, r=r, dataset=dataset)
    bootstrap_node.start()
    print("Forming Chord Ring with Bootstrap Node...")
    bootstrap_node.join(None)
    nodes.append(bootstrap_node)
    
    bootstrap_addr = bootstrap_node.address

    # 2. Spawn and join the remaining nodes one by one
    for i in range(1, num_nodes):
        time.sleep(0.5)
        # Notice we do NOT pass 'r' here. The node must learn it via join()
        node = ChordNode(base_ip, base_port + i, dataset=dataset)
        node.start()
        node.join(bootstrap_addr)
        nodes.append(node)

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
