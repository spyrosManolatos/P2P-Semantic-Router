"""Audits every node's FINGER TABLE against the ground-truth ring.

Why this exists
---------------
`is_finger_stable()` answers "did the last fix_fingers sweep change anything?"
-- a LIVENESS proxy. It cannot distinguish a ring that has converged from one
that has stalled while still wrong, and `ring_healthcheck.py` only probes 8
cluster lookups end-to-end (and, by its own docstring, the semantic router
masks a degraded ring because its probes are ring-adjacent).

At N=550 that blind spot produced measurements that looked healthy -- every
RPC returned, n=500/500 -- while recall FELL as nprobe rose (56.5% -> 43.7%)
and semantic hops hit 64 instead of ~4. Both are what you get when lookups
degrade from O(log N) finger routing to O(N) successor-walking.

This tool answers the CORRECTNESS question directly: for every node n and
every finger i, is finger[i] really the successor of (n + 2^i) mod 2^m?

Run it inside a container so the av-node-* hostnames resolve:

    docker exec av-node-1 python /app/src/benchmarks/containerized/finger_audit.py \
        --containers av-node-1,av-node-2,... --vnodes_per_container 25
"""
import argparse
import sys

try:
    import httpx
except ImportError:  # pragma: no cover - the container always has it
    print("finger_audit: httpx unavailable", file=sys.stderr)
    sys.exit(0)


def rpc(address, method, *args, timeout=60.0):
    resp = httpx.post(f"http://{address}/rpc/{method}",
                      json={"args": args, "kwargs": {}}, timeout=timeout)
    resp.raise_for_status()
    payload = resp.json()
    if "error" in payload:
        raise RuntimeError(f"{method} -> {payload['error']}")
    return payload.get("result")


def vnode_addresses(names, per_container):
    return [f"{n}:{5000 + i}" for n in names for i in range(per_container)]


def true_successor(ring_ids, sorted_ids, target):
    """First node id >= target, wrapping. ring_ids maps id -> address."""
    lo, hi = 0, len(sorted_ids)
    while lo < hi:
        mid = (lo + hi) // 2
        if sorted_ids[mid] < target:
            lo = mid + 1
        else:
            hi = mid
    return ring_ids[sorted_ids[lo % len(sorted_ids)]]


def expand(groups, m):
    """get_finger_table() returns consecutive equal fingers grouped; undo that."""
    table = [None] * m
    for g in groups:
        for i in range(g["from_finger"], g["to_finger"] + 1):
            if 0 <= i < m:
                table[i] = g["node"]
    return table


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--containers", required=True)
    p.add_argument("--vnodes_per_container", type=int, required=True)
    p.add_argument("--m", type=int, default=160, help="Chord id bits (dht.hash_bits_m).")
    p.add_argument("--topn", type=int, default=10, help="Worst offenders to print.")
    p.add_argument("--max_finger", type=int, default=0,
                   help="Only audit fingers below this index. 0 = derive from ring "
                        "size (log2(N)+4); the high fingers all collapse onto the "
                        "same few nodes and add noise, not signal.")
    args = p.parse_args()

    names = [c.strip() for c in args.containers.split(",") if c.strip()]
    addrs = vnode_addresses(names, args.vnodes_per_container)
    entry = addrs[0]

    # Ask a live node to hash each address, so we inherit its exact identity rule
    # (SHA-1 of the RESOLVED IP, not the hostname) rather than reimplementing it.
    ids = {}
    for a in addrs:
        try:
            ids[a] = int(rpc(entry, "get_addr_hash", a))
        except Exception as exc:
            print(f"  could not hash {a}: {exc}")
    if not ids:
        print("finger_audit: no node ids resolved; is the ring up?")
        return 1

    by_id = {v: k for k, v in ids.items()}
    sorted_ids = sorted(by_id)
    n_ring = len(sorted_ids)
    top = args.max_finger or min(args.m, n_ring.bit_length() + 4)

    print(f"finger_audit: {n_ring} nodes, auditing fingers 0..{top - 1} "
          f"(of {args.m}) against the true ring")

    unreachable, wrong_by_node, total_checked, total_wrong = [], {}, 0, 0
    succ_wrong = []

    for a in addrs:
        if a not in ids:
            continue
        try:
            groups = rpc(a, "get_finger_table")
            succ = rpc(a, "get_successor")
        except Exception as exc:
            unreachable.append((a, str(exc)[:60]))
            continue

        nid = ids[a]
        # Successor is finger[0]'s ground truth and the ring's backbone.
        exp_succ = true_successor(by_id, sorted_ids, (nid + 1) % (2 ** args.m))
        if succ != exp_succ:
            succ_wrong.append((a, succ, exp_succ))

        table = expand(groups, args.m)
        bad = 0
        for i in range(top):
            target = (nid + (1 << i)) % (2 ** args.m)
            expected = true_successor(by_id, sorted_ids, target)
            actual = table[i]
            total_checked += 1
            if actual != expected:
                bad += 1
                total_wrong += 1
        if bad:
            wrong_by_node[a] = bad

    print(f"\n  nodes audited      : {n_ring - len(unreachable)}/{n_ring}")
    print(f"  unreachable        : {len(unreachable)}")
    print(f"  fingers checked    : {total_checked}")
    pct = (100.0 * total_wrong / total_checked) if total_checked else 0.0
    print(f"  fingers WRONG      : {total_wrong} ({pct:.1f}%)")
    print(f"  nodes with >=1 bad : {len(wrong_by_node)}")
    print(f"  successors WRONG   : {len(succ_wrong)}")

    if succ_wrong:
        print("\n  -- broken successors (these break the ring backbone, not just routing) --")
        for a, got, exp in succ_wrong[:args.topn]:
            print(f"     {a}: successor={got}  expected={exp}")

    if wrong_by_node:
        print(f"\n  -- worst {args.topn} nodes by bad fingers (of {top} audited) --")
        for a, bad in sorted(wrong_by_node.items(), key=lambda t: -t[1])[:args.topn]:
            print(f"     {a}: {bad}/{top} wrong")

    if unreachable:
        print(f"\n  -- unreachable ({len(unreachable)}) --")
        for a, err in unreachable[:args.topn]:
            print(f"     {a}: {err}")

    # A ring can be a valid cycle (successors fine) yet route terribly, which is
    # exactly the N=550 failure. Report the two independently.
    if not succ_wrong and total_wrong:
        print("\n  VERDICT: successor cycle is INTACT but fingers are WRONG -- lookups "
              "will degrade toward O(N) successor-walking. This is the state that "
              "produces inflated hops and non-monotonic recall while every RPC "
              "still returns successfully.")
    elif not total_wrong:
        print("\n  VERDICT: finger tables are CORRECT.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
