#!/usr/bin/env python3
"""Functional pre-flight check for a virtual-node ring, run INSIDE a container.

Answers a question the convergence gates cannot. Those gates test whether finger
tables have stopped CHANGING; this tests whether they can ROUTE. At N >= 275 the
first question has no useful answer: fix_fingers runs continuously, so a live
ring always has a few nodes mid-update and "all quiescent for 2 polls" is never
observed. The gate times out and proceeds -- which is not by itself wrong.

It matters because the failure it cannot see is ARCHITECTURE-ASYMMETRIC. With
malformed fingers, closest_preceding_node() falls back to the successor and
find_successor terminates ONE HOP OUT, at a node that does not own the key. The
semantic router barely notices (its probes are ring-adjacent, so it walks
successor pointers anyway) while the clustered DHT, needing a real O(log N) jump
per probe, silently retrieves nothing. A degraded ring therefore reads as
"semantic wins overwhelmingly" -- plausible, publishable, and wrong. That exact
failure produced a 2.2%-recall run on 2026-08-21.

This runs BEFORE evaluate.py, so the ring holds no data yet and retrieval cannot
be tested. Routing can: probe cluster ids spread evenly across the id space,
which land far apart on the ring under BOTH placements -- SHA-1 scatters them,
and the semantic linear map puts distant cluster ids at distant ring positions.
Either way they must resolve to several distinct owners via multi-hop lookups.
Collapsing onto one owner at ~0 hops is the dead-finger signature.

Exit 0 = routable, 1 = do not trust this rung.

    python ring_healthcheck.py --arch async_clustered \
        --containers av-bootstrap,av-node-1 --vnodes_per_container 20 --k 5500
"""
import argparse
import sys
import time

try:
    import httpx
except ImportError:  # pragma: no cover - the container always has it
    print("  healthcheck: httpx unavailable, skipping", file=sys.stderr)
    sys.exit(0)


def rpc(address, method, *args, timeout=60.0):
    resp = httpx.post(f"http://{address}/rpc/{method}",
                      json={"args": args, "kwargs": {}}, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"{method} -> {payload['error']}")
    return payload.get("result")


def walk_cycle(entry, ring_size, timeout=15.0):
    """Follow successor pointers from `entry` and count the distinct nodes on the
    cycle. Returns (nodes_seen, is_complete). Complete means the walk came back to
    `entry` having visited every node exactly once -- i.e. one valid ring."""
    seen, cur = set(), entry
    for _ in range(ring_size + 1):
        if cur in seen:
            return len(seen), (cur == entry and len(seen) == ring_size)
        seen.add(cur)
        try:
            cur = rpc(cur, "get_successor", timeout=timeout)
        except Exception:
            return len(seen), False
        if not cur:
            return len(seen), False
    return len(seen), False


def wait_for_cycle(entry, ring_size, deadline_sec, poll=15):
    """Block until the SUCCESSOR RING is complete, before testing whether it can
    ROUTE. These are different questions and they become true at different times:
    the cycle closes first, fingers populate afterwards. Probing routing on a ring
    that has not finished forming measures formation speed, not correctness -- at
    N=550 that made the check a coin flip, failing one arm and passing the other
    minutes apart on identical code."""
    deadline, seen = time.time() + deadline_sec, 0
    while time.time() < deadline:
        seen, complete = walk_cycle(entry, ring_size)
        if complete:
            print(f"  ring FORMED: successor cycle covers all {ring_size} nodes")
            return True
        print(f"  ...waiting for successor ring: {seen}/{ring_size} nodes "
              f"({int(deadline - time.time())}s left)")
        time.sleep(poll)
    print(f"  !! successor ring still incomplete ({seen}/{ring_size}) after "
          f"{deadline_sec}s -- not a routing failure, the ring never formed.")
    return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--arch", required=True)
    p.add_argument("--containers", required=True)
    p.add_argument("--vnodes_per_container", type=int, required=True)
    p.add_argument("--k", type=int, default=5500,
                   help="Cluster count, only used to spread the probe ids.")
    p.add_argument("--probes", type=int, default=8)
    p.add_argument("--wait_cycle_sec", type=int, default=1800,
                   help="Wait up to this long for the SUCCESSOR RING to close "
                        "before probing routing. 0 disables the wait (the old "
                        "behaviour: probe immediately, which at large N tests "
                        "how fast the ring formed rather than whether it works).")
    args = p.parse_args()

    names = [c.strip() for c in args.containers.split(",") if c.strip()]
    entry = f"{names[0]}:5000"
    ring_size = len(names) * args.vnodes_per_container

    # Evenly spaced ids, so they are far apart on the ring under either placement.
    step = max(1, args.k // args.probes)
    cluster_ids = [(i * step) % args.k for i in range(args.probes)]

    if args.wait_cycle_sec > 0 and not wait_for_cycle(entry, ring_size, args.wait_cycle_sec):
        sys.exit(1)

    owners, hop_counts = [], []
    for cid in cluster_ids:
        try:
            chash = rpc(entry, "get_cluster_hash", cid)
            owner, hops = rpc(entry, "find_successor_with_hops", str(chash))
        except Exception as exc:
            print(f"  HEALTHCHECK FAIL: lookup for cluster {cid} raised {exc}")
            sys.exit(1)
        owners.append(owner)
        hop_counts.append(hops)

    distinct = len(set(owners))
    mean_hops = sum(hop_counts) / len(hop_counts)

    print(f"  healthcheck [{args.arch}] entry={entry} ring={ring_size}")
    print(f"    probed clusters : {cluster_ids}")
    print(f"    distinct owners : {distinct} of {len(cluster_ids)}")
    print(f"    mean hops       : {mean_hops:.2f}  (per-probe {hop_counts})")

    # A ring of ring_size nodes cannot spread 8 well-separated keys over fewer
    # than a couple of owners unless routing is broken. Kept deliberately loose:
    # this is a smoke test for a DEAD ring, not a quality bar.
    min_owners = min(3, max(2, ring_size // 2))
    failures = []
    if distinct < min_owners:
        failures.append(f"{len(cluster_ids)} well-separated keys collapsed onto "
                        f"{distinct} owner(s), expected >= {min_owners} -- "
                        "finger tables are not routing")
    if ring_size > 4 and mean_hops < 1.0:
        failures.append(f"mean hops {mean_hops:.2f} < 1.0 -- lookups terminate at "
                        "the successor instead of routing")

    if failures:
        print("  HEALTHCHECK FAIL:")
        for f in failures:
            print(f"    - {f}")
        sys.exit(1)

    print("  healthcheck PASS")
    sys.exit(0)


if __name__ == "__main__":
    main()
