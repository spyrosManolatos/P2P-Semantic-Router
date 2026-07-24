#!/usr/bin/env python3
"""
Virtual-node launcher (Chord virtual nodes).

Separate from app.py (the single-identity launcher) so the original stays intact.
This process hosts `--num_vnodes` independent ring identities, each binding
ip:(port+i) and joining the ring on its own -- i.e. one physical container acts
as N points on the Chord ring (Stoica et al.'s virtual-node technique), used to
build large rings (e.g. 5 containers x 5 vnodes = 25 nodes) on limited hardware.

Only routing HOPS are meaningful in this mode (topological, environment-
independent); wall-clock latency is not, since all vnodes share one interpreter.
"""
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


def build_node(arch, ip, port, bootstrap, dataset):
    """Create, start and join a single ring identity. Returns (node, kind)."""
    if arch == "standard":
        from architectures.standard_dht.node import NaiveChordNode
        # NaiveChordNode starts its server and joins inside __init__.
        node = NaiveChordNode(ip, port, bootstrap_node=bootstrap, dataset=dataset)
        return node, "standard"
    else:
        if arch == "clustered":
            from architectures.clustered_dht.node import ChordNode
        else:  # semantic
            from architectures.semantic_router.node import ChordNode
        node = ChordNode(ip, port, dataset=dataset)
        node.start()
        node.join(bootstrap)
        return node, "chord"


def is_alive(node, kind):
    if kind == "standard":
        return getattr(node, "running", False)
    return not node.shutdown_event.is_set()


def main():
    parser = argparse.ArgumentParser(description="P2P DHT Node service (virtual-node launcher)")
    parser.add_argument("--arch", type=str, default="semantic", choices=["standard", "clustered", "semantic"], help="Topological architecture to run")
    parser.add_argument("--ip", type=str, required=True, help="IP or Hostname this container announces to others")
    parser.add_argument("--port", type=int, default=5000, help="Base port; virtual nodes use port, port+1, ... port+num_vnodes-1")
    parser.add_argument("--bootstrap", type=str, default=None, help="Address of the bootstrap node (host:port)")
    parser.add_argument("--dataset", type=str, default="kaggle", choices=["kaggle", "synthetic"], help="Dataset to load centroids for")
    parser.add_argument("--num_vnodes", type=int, default=1,
                        help="Number of VIRTUAL ring identities to host in this process. "
                             "Each binds ip:(port+i) and joins the ring independently.")
    parser.add_argument("--vnode_stagger_sec", type=float, default=2.0,
                        help="Delay between successive virtual-node joins, so each settles before the next joins.")
    args = parser.parse_args()

    n = max(1, args.num_vnodes)
    print("=" * 60)
    print(f"Starting {args.arch.upper()} DHT container '{args.ip}' with {n} virtual node(s) "
          f"on ports {args.port}..{args.port + n - 1}")
    if args.bootstrap:
        print(f"Joining ring via bootstrap: {args.bootstrap}")
    else:
        print(f"This container hosts the RING INITIALIZER at {args.ip}:{args.port}.")
    print("=" * 60)

    nodes = []  # list of (node, kind)
    for i in range(n):
        p = args.port + i
        # Determine this vnode's bootstrap target:
        #  - initializer container (no --bootstrap): vnode 0 starts the ring (None);
        #    the rest join our own first vnode.
        #  - joining container: every vnode joins the external bootstrap.
        if args.bootstrap is None:
            bs = None if i == 0 else f"{args.ip}:{args.port}"
        else:
            bs = args.bootstrap

        print(f"  [vnode {i}] {args.ip}:{p}  ->  bootstrap={bs or 'SELF (initializer)'}")
        node, kind = build_node(args.arch, args.ip, p, bs, args.dataset)
        nodes.append((node, kind))
        if i < n - 1 and args.vnode_stagger_sec > 0:
            time.sleep(args.vnode_stagger_sec)  # let this identity stabilize before the next joins

    # Graceful shutdown of ALL virtual nodes hosted here.
    def signal_handler(sig, frame):
        print("\nShutting down all virtual nodes gracefully...")
        for node, _ in nodes:
            try:
                node.stop()
            except Exception:
                pass
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"Container running {n} virtual node(s). Press Ctrl+C to terminate.")
    # Block while at least one virtual node is alive.
    while any(is_alive(node, kind) for node, kind in nodes):
        try:
            time.sleep(1)
        except IOError:
            pass


if __name__ == "__main__":
    main()
