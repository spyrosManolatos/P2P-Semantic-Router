#!/usr/bin/env python3
import os
import sys
import time
import argparse
import random
import json

# 1. Resolve project root and append 'src' to Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(project_root, "src"))

# 2. Force CONFIG_PATH to point to our local config file
os.environ["CONFIG_PATH"] = os.path.join(project_root, "local_simulation", "config.local.yaml")

# 3. Import core modules and utilities
from core.config_loader import load_config
from architectures.semantic_router.node import ChordNode
from architectures.semantic_router.simulations.utils import (
    setup_network,
    load_courses,
    inject_courses,
    print_separator,
    print_storage_summary,
    teardown_network
)

def load_simulation_courses():
    """Loads a subset of courses as defined in config.local.yaml to keep the simulation fast."""
    config = load_config()
    limit = config.get("storage", {}).get("num_courses", 100)
    courses = load_courses()
    # Use a fixed seed for simulation reproducibility
    random.seed(42)
    return random.sample(courses, min(limit, len(courses)))

def run_routing_scenario():
    print_separator()
    print("SCENARIO: Distributed Semantic Similarity Routing (KNN)")
    print_separator()
    
    nodes = setup_network()
    courses = load_simulation_courses()
    
    # Show ring mapping
    print_separator()
    print("Chord Ring Topology & Cluster Mappings:")
    print_separator()
    cluster_destinations = {}
    for cid in range(nodes[0].k):
        c_hash = nodes[0].get_cluster_hash(cid)
        dest_node = nodes[0].find_successor(str(c_hash))
        cluster_destinations[cid] = (c_hash, dest_node)

    for node in nodes:
        info = node.get_info()
        print(f"Node {info['address']} (ID: {info['node_id']})")
        mapped_clusters = [cid for cid, (_, dest) in cluster_destinations.items() if dest == node.address]
        print(f"  Responsible for clusters: {mapped_clusters}")
        print("-" * 45)

    # Inject courses
    inject_courses(nodes, courses)
    
    # Test queries
    query_course = random.choice(courses)
    query_json = json.dumps(query_course)
    print(f"\nQuery Course: '{query_course['course_title']}' [{query_course['category']}]")
    
    query_node = random.choice(nodes)
    
    print("\n--- TEST 1: Fast Search (nprobe=1) ---")
    start_time = time.time()
    results_n1 = query_node.get_similar_courses(query_json, nprobe=1)
    dur_n1 = (time.time() - start_time) * 1000
    print(f"nprobe=1 returned {len(results_n1)} courses in {dur_n1:.1f}ms")
    for idx, r_json in enumerate(results_n1[:3]):
        r = json.loads(r_json)
        print(f"  - {r['course_title']} (Similarity: {r.get('similarity', 0.0):.4f})")
    
    print("\n--- TEST 2: High Recall Search (nprobe=2 Fanout) ---")
    start_time = time.time()
    results_n2 = query_node.get_similar_courses(query_json, nprobe=2)
    dur_n2 = (time.time() - start_time) * 1000
    print(f"nprobe=2 returned {len(results_n2)} courses in {dur_n2:.1f}ms")
    for idx, r_json in enumerate(results_n2[:3]):
        r = json.loads(r_json)
        print(f"  - {r['course_title']} (Similarity: {r.get('similarity', 0.0):.4f})")
        
    teardown_network(nodes)


def run_failure_scenario():
    print_separator()
    print("SCENARIO: Node Failure, Replication & Self-Healing")
    print_separator()
    
    nodes = setup_network(num_nodes=4)
    courses = load_simulation_courses()
    inject_courses(nodes, courses)
    
    print_storage_summary(nodes, title="Storage Summary BEFORE Failure")
    
    # Kill node on port 8002
    target_failed_node = next((n for n in nodes if n.port == 8002), None)
    if target_failed_node:
        print_separator()
        print(f"Killing Node {target_failed_node.address}...")
        print_separator()
        target_failed_node.stop()
        nodes.remove(target_failed_node)
        
        print("Waiting 8 seconds for ring stabilization (self-healing)...")
        time.sleep(8.0)
        
        print_storage_summary(nodes, title="Storage Summary AFTER Failure")
        
        # Verify query still works
        query_course = random.choice(courses)
        query_json = json.dumps(query_course)
        query_node = random.choice(nodes)
        
        print(f"\nAttempting search AFTER failure via Node {query_node.address}...")
        results = query_node.get_similar_courses(query_json, nprobe=2)
        print(f"Successfully retrieved {len(results)} courses from surviving nodes.")
        
    teardown_network(nodes)


def run_join_scenario():
    print_separator()
    print("SCENARIO: Node Joining & Automatic Data Migration")
    print_separator()
    
    nodes = setup_network(num_nodes=3)
    courses = load_simulation_courses()
    inject_courses(nodes, courses)
    
    print_storage_summary(nodes, title="Storage Summary BEFORE Node 4 Joins")
    
    # Spin up Node 4
    print_separator()
    print("Spinning up Node 4 (port 8004) and joining the network...")
    print_separator()
    
    node4 = ChordNode("127.0.0.1", 8004)
    node4.start()
    nodes.append(node4)
    
    # Join
    node4.join(nodes[0].address)
    
    print("Waiting 8 seconds for Chord stabilization and data migration...")
    time.sleep(8.0)
    
    print_storage_summary(nodes, title="Storage Summary AFTER Node 4 Joined")
    
    info4 = node4.get_info()
    primary_count = sum(info4['primary_summary'].values())
    print(f"\nVerification: Node 4 is hosting {primary_count} primary courses migrated from other nodes.")
    
    teardown_network(nodes)


def run_boot_only():
    print_separator()
    print("SCENARIO: Booting Local DHT Network Only")
    print_separator()
    
    config = load_config()
    num_nodes = config['network']['number_of_nodes']
    print(f"Booting {num_nodes} semantic nodes...")
    nodes = setup_network()
    
    courses = load_simulation_courses()
    inject_courses(nodes, courses)
    
    print("\nNetwork booted successfully!")
    print("Addresses of active nodes:")
    for n in nodes:
        print(f"  - http://{n.address}")
        
    print("\nPress Ctrl+C to terminate the network.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down network...")
    finally:
        teardown_network(nodes)


def main():
    parser = argparse.ArgumentParser(description="P2P Semantic Router Local Simulation CLI")
    parser.add_argument(
        "--scenario",
        type=str,
        default="routing",
        choices=["routing", "failure", "join", "boot"],
        help="The simulation scenario to run"
    )
    args = parser.parse_args()
    
    if args.scenario == "routing":
        run_routing_scenario()
    elif args.scenario == "failure":
        run_failure_scenario()
    elif args.scenario == "join":
        run_join_scenario()
    elif args.scenario == "boot":
        run_boot_only()

if __name__ == "__main__":
    main()
