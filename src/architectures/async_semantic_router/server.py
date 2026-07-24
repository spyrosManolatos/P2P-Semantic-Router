from core.async_node_server import run_node as _run_node
from architectures.async_semantic_router.node import ChordNode


def run_node(ip: str, port: int, bootstrap, dataset: str):
    node = ChordNode(ip, port, dataset=dataset)
    _run_node(node, ip, port, bootstrap, label="ASYNC SEMANTIC ROUTER")
