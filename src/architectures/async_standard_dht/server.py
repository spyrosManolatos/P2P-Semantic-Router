from core.async_node_server import run_node as _run_node
from architectures.async_standard_dht.node import NaiveChordNode


def run_node(ip: str, port: int, bootstrap, dataset: str):
    node = NaiveChordNode(ip, port, dataset=dataset)
    _run_node(node, ip, port, bootstrap, label="ASYNC STANDARD DHT")
