import json
import time
import sys
import os

# Add project root to path for imports
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))

from src.naive.simulations.utils import setup_network, teardown_network, load_courses
from src.config_loader import load_config
from src.naive.node import NaiveChordNode

def run():
    print("=== Simulation 01: Naive Routing (Baseline) ===")
    config = load_config()
    num_nodes = config['network']['number_of_nodes']
    base_port = 9000
    nodes = []
    
    try:
        # Step 1: Start Bootstrap Node
        print("Starting Bootstrap Node...")
        boot_node = NaiveChordNode("127.0.0.1", base_port)
        nodes.append(boot_node)
        
        courses = load_courses()
        print(f"Injecting {len(courses)} courses into Bootstrap Node...")
        for c in courses:
            boot_node.put_course(json.dumps(c))
            
        print(f"Bootstrap Node holds {len(boot_node.storage)} courses initially.")
        
        # Step 2: Start other nodes sequentially to trigger data migration
        print("Starting remaining nodes to trigger data migration...")
        for i in range(1, num_nodes):
            port = base_port + i
            print(f"Node 127.0.0.1:{port} joining...")
            node = NaiveChordNode("127.0.0.1", port, bootstrap_node=f"127.0.0.1:{base_port}")
            nodes.append(node)
            time.sleep(3) # Wait for stabilization and migration
            
        print("Waiting 10 more seconds for final stabilization and full finger tables...")
        time.sleep(10)
        
        print("\n--- Network Ring Topology ---")
        for n in nodes:
            info = n.get_info()
            print(f"Node {info['address']} (ID: {info['node_id']}) -> Successor: {info['successor']} | Predecessor: {info['predecessor']}")
        print("-" * 50)
        
        print("Data migration complete. Let's see how many courses each node holds:")
        for n in nodes:
            info = n.get_info()
            print(f"Node {n.address} holds {info['primary_count']} PRIMARY courses and {info['replica_count']} REPLICAS.")
        print("-" * 50)
        
        # Test 1: Similarity Search
        query_course = courses[0] # Let's search for similar courses to the first one
        print(f"Searching for courses similar to: {query_course['course_title']}")
        
        start_time = time.time()
        # Random node performs the search
        results_json = nodes[0].get_similar_courses(json.dumps(query_course))
        end_time = time.time()
        
        print(f"\\nTop 5 Results (Latency: {end_time - start_time:.4f} seconds):")
        for i, res_str in enumerate(results_json):
            c = json.loads(res_str)
            print(f" {i+1}. {c['course_title']} (ID: {c['course_id']}) [Similarity: {c.get('similarity', 0.0):.4f}]")
            
        print("\\nNotice the [NAIVE SEARCH] print showing that it had to visit almost ALL nodes to find the answer!")
        
    finally:
        teardown_network(nodes)

if __name__ == "__main__":
    run()
