import os
import sys
import json
import time
from typing import List

# Add the root 'src/' directory to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
# Add the architecture directory so we can import 'node'
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from architectures.standard_dht.node import NaiveChordNode
from core.config_loader import load_config

def load_courses(dataset="kaggle") -> List[dict]:
    config = load_config()
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    if dataset == "synthetic":
        data_path = os.path.join(project_root, config['storage']['data']['synthetic_path'])
    else:
        data_path = os.path.join(project_root, config['storage']['data']['kaggle']['normalized_path'])
        
    if not os.path.exists(data_path):
        data_path = config['storage']['data']['synthetic_path']
        
    with open(data_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def setup_network(num_nodes: int, base_port: int, dataset="kaggle") -> List[NaiveChordNode]:
    print(f"Setting up Naive DHT network with {num_nodes} nodes...")
    nodes = []
    
    boot_node = NaiveChordNode("127.0.0.1", base_port, dataset=dataset)
    nodes.append(boot_node)
    
    for i in range(1, num_nodes):
        port = base_port + i
        node = NaiveChordNode("127.0.0.1", port, bootstrap_node=f"127.0.0.1:{base_port}", dataset=dataset)
        nodes.append(node)
        time.sleep(0.5)
        
    print("Waiting 10 seconds for full ring stabilization and finger tables to populate...")
    time.sleep(10)
    return nodes

def teardown_network(nodes: List[NaiveChordNode]):
    for n in nodes:
        n.stop()
