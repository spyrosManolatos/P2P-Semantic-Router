"""Standalone data injection for demos.

Loads the normalized Kaggle courses and puts them into the DHT via a single
node (the ring distributes them to the responsible peers). Use this to fill
the ring before querying through the API gateway, without running benchmarks.

    docker compose exec gateway python src/scripts/inject_data.py
"""
import argparse
import json
import os
import sys
import xmlrpc.client

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.config_loader import load_config


def main():
    parser = argparse.ArgumentParser(description="Inject courses into a running DHT ring")
    parser.add_argument("--node", type=str, default="bootstrap-node:5000", help="Any ring node (host:port)")
    parser.add_argument("--limit", type=int, default=500, help="Number of courses to inject (0 = all)")
    parser.add_argument("--transport", choices=["xmlrpc", "json"], default="xmlrpc",
                        help="xmlrpc for the sync stacks; json for the async (FastAPI/httpx) stacks")
    args = parser.parse_args()

    config = load_config()
    dataset_path = config["storage"]["data"]["kaggle"]["normalized_path"]
    with open(dataset_path) as f:
        courses = json.load(f)
    if args.limit > 0:
        courses = courses[: args.limit]

    if args.transport == "json":
        from core.json_rpc_client import JSONRPCProxy
        client = JSONRPCProxy(args.node)
    else:
        client = xmlrpc.client.ServerProxy(f"http://{args.node}", allow_none=True)
    print(f"Injecting {len(courses)} courses via {args.node}...")
    for i, course in enumerate(courses):
        client.put_course(json.dumps(course))
        if (i + 1) % 100 == 0:
            print(f"  {i + 1}/{len(courses)}")
    print("Done. The ring is ready for queries.")


if __name__ == "__main__":
    main()
