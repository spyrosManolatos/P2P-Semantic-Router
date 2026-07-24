#!/usr/bin/env python3
import os
import sys
import time
import argparse
import signal

# 1. Resolve project root and append 'src' to Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(project_root, "src"))

# 2. Force CONFIG_PATH to point to our production container config file
if not os.environ.get("CONFIG_PATH"):
    os.environ["CONFIG_PATH"] = os.path.join(project_root, "containerized_environment", "config.prod.yaml")

def main():
    parser = argparse.ArgumentParser(description="P2P DHT Node service")
    parser.add_argument("--arch", type=str, default="semantic",
                        choices=["standard", "clustered", "semantic",
                                 "async_standard", "async_clustered", "async_semantic"],
                        help="Topological architecture to run")
    parser.add_argument("--ip", type=str, required=True, help="IP or Hostname this node announces to others")
    parser.add_argument("--port", type=int, default=5000, help="Port this node listens on")
    parser.add_argument("--bootstrap", type=str, default=None, help="Address of the bootstrap node (host:port)")
    parser.add_argument("--dataset", type=str, default="kaggle", choices=["kaggle", "synthetic"], help="Dataset to load centroids for")
    args = parser.parse_args()

    print("=" * 60)
    print(f"Starting {args.arch.upper()} DHT Node on announce address {args.ip}:{args.port}")
    if args.bootstrap:
        print(f"Bootstrapping via node: {args.bootstrap}")
    else:
        print("Starting as Bootstrap (Ring Initializer) Node.")
    print("=" * 60)

    # Initialize the specific node architecture
    if args.arch == "standard":
        from architectures.standard_dht.node import NaiveChordNode
        # NaiveChordNode starts and joins automatically inside __init__
        node = NaiveChordNode(args.ip, args.port, bootstrap_node=args.bootstrap, dataset=args.dataset)
        
        # Shutdown hook
        def signal_handler(sig, frame):
            print("\nShutting down node gracefully...")
            node.stop()
            sys.exit(0)
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        print("Node service is running. Press Ctrl+C to terminate.")
        # Block standard node
        while node.running:
            try:
                time.sleep(1)
            except IOError:
                pass

    elif args.arch in ("async_standard", "async_clustered", "async_semantic"):
        # uvicorn.Server.run() owns the event loop and blocks until shutdown;
        # join() and the periodic stabilization loop run inside it (see
        # core.async_node_server.build_app's lifespan).
        if args.arch == "async_standard":
            from architectures.async_standard_dht.server import run_node
        elif args.arch == "async_clustered":
            from architectures.async_clustered_dht.server import run_node
        else:
            from architectures.async_semantic_router.server import run_node
        run_node(args.ip, args.port, args.bootstrap, args.dataset)

    else:
        if args.arch == "clustered":
            from architectures.clustered_dht.node import ChordNode
        else: # semantic
            from architectures.semantic_router.node import ChordNode

        node = ChordNode(args.ip, args.port, dataset=args.dataset)
        
        # Shutdown hook
        def signal_handler(sig, frame):
            print("\nShutting down node gracefully...")
            node.stop()
            sys.exit(0)

        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)

        node.start()
        node.join(args.bootstrap)

        print("Node service is running. Press Ctrl+C to terminate.")
        # Block clustered/semantic node
        while not node.shutdown_event.is_set():
            try:
                time.sleep(1)
            except IOError:
                pass

if __name__ == "__main__":
    main()
