import json
import random
import time
import sys
import os

# Add the parent directory and test directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from node import ChordNode
from utils import (
    setup_network, 
    load_courses, 
    inject_courses, 
    print_separator, 
    print_storage_summary, 
    teardown_network
)

def main():
    # 1. Start with a 3-node network
    nodes = setup_network(num_nodes=3, base_port=8001)
    courses = load_courses()
    
    # 2. Inject courses into the network
    inject_courses(nodes, courses)
    
    # 3. Print storage summary before the new node joins
    print_storage_summary(nodes, title="Storage Summary BEFORE Node 4 Joins")
    
    # 4. Spin up the 4th node
    print_separator()
    print("Spinning up Node 4 (port 8004) and joining the network...")
    print_separator()
    
    node4 = ChordNode("127.0.0.1", 8004)
    node4.start()
    nodes.append(node4)
    
    # Bootstrap via Node 1
    bootstrap_addr = nodes[0].address
    node4.join(bootstrap_addr)
    
    # 5. Wait for stabilization and migration to settle
    print("\nWaiting 8 seconds for Chord stabilization and data migration...")
    time.sleep(8.0)
    
    # 6. Print storage summary after the new node joins
    print_storage_summary(nodes, title="Storage Summary AFTER Node 4 Joins")
    
    # 7. Verification of data migration
    info4 = node4.get_info()
    print_separator()
    print("Verification Report for Joining Node 4:")
    print_separator()
    print(f"Address:            {info4['address']}")
    print(f"Node ID:            {info4['node_id']}")
    print(f"Predecessor:        {info4['predecessor']}")
    print(f"Successor:          {info4['successor']}")
    print(f"Primary Summary:    {info4['primary_summary']}")
    print(f"Replica Summary:    {info4['replica_summary']}")
    
    primary_count = sum(info4['primary_summary'].values())
    replica_count = sum(info4['replica_summary'].values())
    total_count = primary_count + replica_count
    
    print(f"\n=> Node 4 is hosting {primary_count} primary courses and {replica_count} replica courses.")
    
    # Verify that query retrieval works from the newly joined node
    query_course = random.choice(courses)
    query_json = json.dumps(query_course)
    print(f"\nAttempting to retrieve similar courses for '{query_course['course_title']}' via newly joined Node 4...")
    
    start_time = time.time()
    results = node4.get_similar_courses(query_json, nprobe=2)
    duration = (time.time() - start_time) * 1000
    
    print(f"Query returned {len(results)} courses in {duration:.1f}ms.")
    
    if total_count > 0 and len(results) > 0:
        print("\n=> SUCCESS: Data migration on node join completed successfully and data is queryable!")
    else:
        print("\n=> FAILURE: Data migration on node join did not succeed or data is unqueryable.")
        
    teardown_network(nodes)

if __name__ == "__main__":
    main()
