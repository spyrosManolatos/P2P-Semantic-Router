import json
import random
import time
from utils import (
    setup_network, 
    load_courses, 
    inject_courses, 
    print_separator, 
    teardown_network
)

def main():
    nodes = setup_network(num_nodes=4)
    courses = load_courses()
    
    inject_courses(nodes, courses)
    
    query_course = random.choice(courses)
    query_json = json.dumps(query_course)
    
    print_separator()
    print("Simulating Query Overload (Active Replica Load Balancing)...")
    print_separator()
    
    # Blast the network with 6 rapid-fire queries for the same course
    # This will hit the Primary node and push its query_load over the threshold!
    query_node = random.choice(nodes)
    print(f"Blasting query node {query_node.address} with 6 rapid-fire similarity queries...")
    
    for i in range(1, 7):
        start_time = time.time()
        # nprobe=1 so we strictly hit the specific Primary node responsible for this cluster
        results = query_node.get_similar_courses(query_json, nprobe=1)
        dur = (time.time() - start_time) * 1000
        print(f"  [Query {i}/6] Returned {len(results)} courses in {dur:.1f}ms")
        # Tiny sleep to ensure RPC completes without socket reset, but fast enough to outpace cooldown
        time.sleep(0.05)

    teardown_network(nodes)

if __name__ == "__main__":
    main()
