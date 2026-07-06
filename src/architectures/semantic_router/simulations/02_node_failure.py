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
    nodes = setup_network(num_nodes=4)
    courses = load_courses()
    
    inject_courses(nodes, courses)
    
    print_separator()
    print("Old Chord Ring Topology (Before Failure):")
    print_separator()
    for node in nodes:
        info = node.get_info()
        print(f"Address: {info['address']} -> Successor: {info['successor']}")
        
    print_storage_summary(nodes, title="Storage Summary BEFORE Failure")
        
    query_course = random.choice(courses)
    query_json = json.dumps(query_course)
    
    # Query BEFORE failure
    query_node_before = random.choice(nodes)
    print(f"\nPerforming similarity search BEFORE failure via Node {query_node_before.address}...")
    results_before = query_node_before.get_similar_courses(query_json)
    print("Top 5 Results BEFORE failure:")
    for idx, r_json in enumerate(results_before):
        r = json.loads(r_json)
        print(f"  {idx + 1}. {r['course_title']} (ID: {r['course_id']}) [Similarity: {r.get('similarity', 0.0):.4f}]")
    
    # 8. Simulate Node Failure and verify Replication
    print_separator()
    print("Simulating Node Failure to Test Replication (Fault Tolerance)...")
    print_separator()
    
    # Kill node on port 8002
    target_failed_node = next((n for n in nodes if n.port == 8002), None)
    if target_failed_node:
        print(f"Killing Node {target_failed_node.address}.")
        target_failed_node.stop()
        nodes.remove(target_failed_node)
        
        print("\nWaiting 8 seconds for ring stabilization (self-healing)...")
        time.sleep(8.0)
        
        print_separator()
        print("New Chord Ring Topology (Recovered):")
        print_separator()
        for node in nodes:
            info = node.get_info()
            print(f"Address: {info['address']} -> Successor: {info['successor']}")

        print_storage_summary(nodes, title="Storage Summary AFTER Failure (Replicas Upgraded and Healed!)")

        # Verify replication factor recovery
        all_primaries = set()
        all_replicas = set()
        for node in nodes:
            info = node.get_info()
            all_primaries.update(info['primary_summary'].keys())
            all_replicas.update(info['replica_summary'].keys())
            
        print("\nReplication Recovery Validation:")
        print(f"  Primary clusters in network: {sorted(list(all_primaries))}")
        print(f"  Replica clusters in network: {sorted(list(all_replicas))}")
        
        expected_clusters = {str(i) for i in range(nodes[0].k)}
        replication_healed = (all_primaries == expected_clusters and all_replicas == expected_clusters)

        query_node_after = random.choice(nodes)
        print(f"\nAttempting to retrieve similar courses AFTER failure via surviving Node {query_node_after.address}...")
        
        start_time = time.time()
        results_after = query_node_after.get_similar_courses(query_json)
        duration = (time.time() - start_time) * 1000
        
        print("\nTop 5 Results AFTER failure:")
        for idx, r_json in enumerate(results_after):
            r = json.loads(r_json)
            print(f"  {idx + 1}. {r['course_title']} (ID: {r['course_id']}) [Similarity: {r.get('similarity', 0.0):.4f}]")

        print(f"\nQuery returned {len(results_after)} courses in {duration:.1f}ms.")
        
        # Verify recall
        titles_before = [json.loads(r)['course_id'] for r in results_before]
        titles_after = [json.loads(r)['course_id'] for r in results_after]
        recall_matched = (titles_before == titles_after and len(titles_after) > 0)

        if recall_matched and replication_healed:
            print("=> SUCCESS: Data was successfully retrieved, recall is identical, and replication factor (R=1) was fully healed!")
        else:
            print(f"=> FAILURE: Data retrieval, recall, or replica healing failed. (Recall matched: {recall_matched}, Replication healed: {replication_healed})")

    teardown_network(nodes)

if __name__ == "__main__":
    main()
