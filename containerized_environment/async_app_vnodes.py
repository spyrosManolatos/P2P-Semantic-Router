#!/usr/bin/env python3
"""
Async virtual-node launcher (Chord virtual nodes) -- the async_* counterpart of
app_vnodes.py. One physical container hosts `--num_vnodes` independent ring
identities, each on its own uvicorn.Server bound to ip:(port+i), all cooperatively
scheduled on ONE shared asyncio event loop for the process (instead of one OS
thread per identity as in the xmlrpc-based app_vnodes.py).

Only routing HOPS are meaningful in this mode (topological, environment-
independent); wall-clock latency is not, since all vnodes share one interpreter
and one event loop.
"""
import os
import sys
import asyncio
import argparse

# 1. Resolve project root and append 'src' to Python path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(project_root, "src"))

# 2. Force CONFIG_PATH to point to our production container config file
if not os.environ.get("CONFIG_PATH"):
    os.environ["CONFIG_PATH"] = os.path.join(project_root, "containerized_environment", "config.prod.yaml")

import uvicorn
from core.async_node_server import build_app


def build_node_instance(arch, ip, port, dataset):
    if arch == "async_standard":
        from architectures.async_standard_dht.node import NaiveChordNode
        return NaiveChordNode(ip, port, dataset=dataset)
    elif arch == "async_clustered":
        from architectures.async_clustered_dht.node import ChordNode
        return ChordNode(ip, port, dataset=dataset)
    else:  # async_semantic
        from architectures.async_semantic_router.node import ChordNode
        return ChordNode(ip, port, dataset=dataset)


async def main_async(args):
    n = max(1, args.num_vnodes)
    print("=" * 60)
    print(f"Starting {args.arch.upper()} DHT container '{args.ip}' with {n} virtual node(s) "
          f"on ports {args.port}..{args.port + n - 1}")
    if args.bootstrap:
        print(f"Joining ring via bootstrap: {args.bootstrap}")
    else:
        print(f"This container hosts the RING INITIALIZER at {args.ip}:{args.port}.")
    print("=" * 60)

    bind_ip = args.ip if args.ip in ("127.0.0.1", "localhost") else "0.0.0.0"
    tasks = []
    for i in range(n):
        p = args.port + i
        if args.bootstrap is None:
            bs = None if i == 0 else f"{args.ip}:{args.port}"
        else:
            bs = args.bootstrap

        print(f"  [vnode {i}] {args.ip}:{p}  ->  bootstrap={bs or 'SELF (initializer)'}")
        node = build_node_instance(args.arch, args.ip, p, args.dataset)
        app = build_app(node, bs)
        config = uvicorn.Config(app, host=bind_ip, port=p, log_level="warning")
        server = uvicorn.Server(config)
        node._uvicorn_server = server
        tasks.append(asyncio.create_task(server.serve()))

        if i < n - 1 and args.vnode_stagger_sec > 0:
            # Let this identity's join()/lifespan-startup begin settling before
            # the next one starts joining (approximation of the sync launcher's
            # thread-stagger; join() itself runs inside server.serve()'s startup).
            await asyncio.sleep(args.vnode_stagger_sec)

    print(f"Container running {n} virtual node(s). Awaiting shutdown...")
    await asyncio.gather(*tasks)


def main():
    parser = argparse.ArgumentParser(description="Async P2P DHT Node service (virtual-node launcher)")
    parser.add_argument("--arch", type=str, default="async_semantic",
                        choices=["async_standard", "async_clustered", "async_semantic"],
                        help="Topological architecture to run")
    parser.add_argument("--ip", type=str, required=True, help="IP or Hostname this container announces to others")
    parser.add_argument("--port", type=int, default=5000, help="Base port; virtual nodes use port, port+1, ... port+num_vnodes-1")
    parser.add_argument("--bootstrap", type=str, default=None, help="Address of the bootstrap node (host:port)")
    parser.add_argument("--dataset", type=str, default="kaggle", choices=["kaggle", "synthetic"], help="Dataset to load centroids for")
    parser.add_argument("--num_vnodes", type=int, default=1,
                        help="Number of VIRTUAL ring identities to host in this process. "
                             "Each binds ip:(port+i) and joins the ring independently.")
    parser.add_argument("--vnode_stagger_sec", type=float, default=2.0,
                        help="Delay between successive virtual-node startups, so each settles before the next joins.")
    args = parser.parse_args()

    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
