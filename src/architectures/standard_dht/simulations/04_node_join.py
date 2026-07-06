import json
import time
import sys
import os

from utils import setup_network, teardown_network, load_courses
from node import NaiveChordNode

def run():
    print("=== Simulation 04: Naive Node Join & Data Migration ===")
    base_port = 9000
    nodes = setup_network(num_nodes=3, base_port=base_port)
    
    try:
        courses = load_courses()
        print(f"Injecting {len(courses)} courses into the network...")
        for c in courses:
            nodes[0].put_course(json.dumps(c))
            
        print("-" * 50)
        print("Storage Summary BEFORE Node 4 Joins:")
        print("-" * 50)
        for n in nodes:
            info = n.get_info()
            print(f"Node {n.address} holds {info['primary_count']} PRIMARY courses and {info['replica_count']} REPLICAS.")
        print("-" * 50)
        
        # Perform similarity search BEFORE join
        query_course = courses[0]
        print(f"Performing similarity search BEFORE join for: '{query_course['course_title']}'")
        results_before = nodes[0].get_similar_courses(json.dumps(query_course))
        print("\nTop 5 Results BEFORE join:")
        for idx, r_json in enumerate(results_before):
            r = json.loads(r_json)
            print(f"  {idx + 1}. {r['course_title']} (ID: {r['course_id']}) [Similarity: {r.get('similarity', 0.0):.4f}]")
        print("-" * 50)
        
        # Step 2: Spin up the 4th node
        print("Spinning up Node 4 (port 9003) and joining the network...")
        print("-" * 50)
        node4 = NaiveChordNode("127.0.0.1", 9003)
        nodes.append(node4)
        
        # Bootstrap via Node 1
        bootstrap_addr = f"127.0.0.1:{base_port}"
        node4.join(bootstrap_addr)
        
        print("\nWaiting 10 seconds for Chord stabilization and data migration...")
        time.sleep(10.0)
        
        print("-" * 50)
        print("Storage Summary AFTER Node 4 Joins:")
        print("-" * 50)
        for n in nodes:
            info = n.get_info()
            print(f"Node {n.address} holds {info['primary_count']} PRIMARY courses and {info['replica_count']} REPLICAS.")
        print("-" * 50)
        
        # Verify that query retrieval works from the newly joined node with same recall
        print(f"Performing similarity search AFTER join via newly joined Node 4...")
        results_after = node4.get_similar_courses(json.dumps(query_course))
        print("\nTop 5 Results AFTER join:")
        for idx, r_json in enumerate(results_after):
            r = json.loads(r_json)
            print(f"  {idx + 1}. {r['course_title']} (ID: {r['course_id']}) [Similarity: {r.get('similarity', 0.0):.4f}]")
        print("-" * 50)
        
        # Verify correctness
        titles_before = [json.loads(r)['course_id'] for r in results_before]
        titles_after = [json.loads(r)['course_id'] for r in results_after]
        if titles_before == titles_after and len(titles_after) > 0:
            print("=> SUCCESS: Data migration on node join completed successfully and recall is identical!")
        else:
            print("=> FAILURE: Data migration failed or query recall is not matching.")
            
    finally:
        teardown_network(nodes)

if __name__ == "__main__":
    run()
