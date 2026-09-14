#!/usr/bin/env python3
"""Per-ring reader for the shared ingestion log (stage 2).

One of these runs inside EACH ring's namespace. It tails the shared append-only
log, hands every record to its own ring via put_course, and persists its own
byte offset. The two readers are the same code with the same arguments; the only
difference between them is which ring's DNS namespace they resolve `dht` in --
so the two rings ingest an identical document stream and disagree only where
their artifacts disagree.

Three properties are deliberate:

  * It holds NO membership. The endpoint is one NAME (`dht:5000`, the ring's own
    headless Service); the ring publishes its own members and the reader takes
    whatever DNS returns. Nothing here is a peer directory, so this does not
    become the central routing authority A1 forbids -- exactly the same argument
    that applies to the ExternalName entry point in stage 1.

  * It does NOT compute placement. It calls put_course as-is and lets the
    contacted NODE vectorize, pick the cluster and route. Placement is a
    property of the ring's artifact, which is the thing under test; computing it
    reader-side would silently make both rings agree.

  * The cursor is committed AFTER the record is applied, so a crash re-delivers
    at most one record. Redelivery is safe because store_replica keys storage by
    course_id -- re-applying a record overwrites it in place rather than
    duplicating it. At-least-once plus an idempotent sink is exactly-once in
    effect, without needing a transaction.

    python src/scripts/queue_reader.py --ring v0 --endpoint dht:5000
"""
import argparse
import json
import os
import socket
import sys
import time
import xmlrpc.client
from datetime import datetime, timezone

READ_CHUNK = 1 << 20


def log(msg: str):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}", flush=True)


def load_cursor(path: str, ring: str) -> dict:
    try:
        with open(path) as f:
            c = json.load(f)
        return {"ring": ring, "byte": int(c.get("byte", 0)), "line": int(c.get("line", 0)),
                "applied": int(c.get("applied", 0)), "failed": int(c.get("failed", 0))}
    except (FileNotFoundError, ValueError, KeyError):
        return {"ring": ring, "byte": 0, "line": 0, "applied": 0, "failed": 0}


def save_cursor(path: str, cursor: dict):
    """Atomic commit: a torn cursor file would make the reader replay or, worse,
    skip records. Write a temp file, fsync it, then rename over the target."""
    cursor = dict(cursor, updated=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(cursor, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def stable_peers(endpoint: str):
    """Resolve the ring's headless Service and keep only peers whose finger table
    has converged.

    The headless Service sets publishNotReadyAddresses, so DNS also returns pods
    that are still joining. Routing a PUT through a node whose fingers have not
    converged can land the record on the wrong successor, so the reader applies
    the SAME predicate the readiness probe uses (is_finger_stable) before
    trusting a peer.
    """
    host, _, port = endpoint.partition(":")
    port = int(port or 5000)
    try:
        ips = sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)})
    except socket.gaierror:
        return []
    peers = []
    for ip in ips:
        try:
            proxy = xmlrpc.client.ServerProxy(f"http://{ip}:{port}", allow_none=True)
            if proxy.is_finger_stable():
                peers.append((ip, proxy))
        except Exception:
            continue
    return peers


def main():
    p = argparse.ArgumentParser(description="Tail the shared ingestion log into one ring")
    p.add_argument("--ring", default=os.environ.get("RING_VERSION", "unset"))
    p.add_argument("--queue", default="/queue/courses.jsonl")
    p.add_argument("--cursor", default="/cursor/offset.json")
    p.add_argument("--endpoint", default="dht:5000",
                   help="ONE name -- the ring's own headless Service. Never a peer list.")
    p.add_argument("--poll", type=float, default=2.0, help="Seconds to wait when the log has no new records")
    p.add_argument("--batch", type=int, default=100, help="Max records applied before committing the cursor")
    p.add_argument("--timeout", type=float, default=20.0, help="RPC timeout per put_course")
    args = p.parse_args()

    socket.setdefaulttimeout(args.timeout)
    cursor = load_cursor(args.cursor, args.ring)
    log(f"ring={args.ring} endpoint={args.endpoint} queue={args.queue}")
    log(f"resuming at line {cursor['line']} (byte {cursor['byte']}), {cursor['applied']} applied so far")

    rr = 0
    idle_logged = False

    while True:
        if not os.path.exists(args.queue):
            if not idle_logged:
                log("waiting for the log to appear (nothing published yet)")
                idle_logged = True
            time.sleep(args.poll)
            continue

        size = os.path.getsize(args.queue)
        if size < cursor["byte"]:
            # The log shrank: it was truncated or replaced. Replaying from 0 is
            # the safe response -- re-applying records is a no-op (keyed by
            # course_id), whereas keeping a stale offset would skip real data.
            log(f"log shrank ({size} < {cursor['byte']}) -- treating as a reset, replaying from 0")
            cursor.update(byte=0, line=0)
            save_cursor(args.cursor, cursor)

        if size == cursor["byte"]:
            if not idle_logged:
                log(f"caught up at line {cursor['line']}")
                idle_logged = True
            time.sleep(args.poll)
            continue

        with open(args.queue, "rb") as f:
            f.seek(cursor["byte"])
            chunk = f.read(READ_CHUNK)

        # A producer may be mid-append. Only whole lines are records; anything
        # after the last newline is a partial write and is left for next pass.
        cut = chunk.rfind(b"\n")
        if cut < 0:
            time.sleep(args.poll)
            continue
        # Split on b"\n" and nothing else. str.splitlines() would ALSO split on
        # \x0b, \x1c-\x1e and U+2028/U+2029, and course descriptions come from a
        # scraped CSV -- one such character inside a record would break it in two
        # and put the byte cursor permanently out of step with the log.
        records = chunk[:cut + 1].split(b"\n")[:-1]

        peers = stable_peers(args.endpoint)
        if not peers:
            log(f"no finger-stable peer at {args.endpoint} yet -- holding at line {cursor['line']}")
            time.sleep(args.poll)
            continue

        idle_logged = False
        applied = 0
        consumed_bytes = 0
        t0 = time.time()

        for raw in records[:args.batch]:
            line_bytes = len(raw) + 1        # + the newline that terminated it
            record = raw.decode("utf-8")
            ok = False
            # Try every peer once before giving up on this record: a single pod
            # restarting must not stall the whole ingest.
            for attempt in range(len(peers)):
                ip, proxy = peers[(rr + attempt) % len(peers)]
                try:
                    ok = bool(proxy.put_course(record))
                    if ok:
                        break
                except Exception as e:
                    log(f"put_course via {ip} failed: {type(e).__name__}: {e}")
            rr += 1

            if not ok:
                # Do NOT advance the cursor past a record that was not applied.
                # Stopping here keeps the log's ordering guarantee meaningful:
                # every record before the cursor is in the ring.
                cursor["failed"] += 1
                log(f"record at line {cursor['line'] + applied} not applied by any peer -- pausing")
                break

            applied += 1
            consumed_bytes += line_bytes

        if applied:
            cursor["byte"] += consumed_bytes
            cursor["line"] += applied
            cursor["applied"] += applied
            save_cursor(args.cursor, cursor)
            rate = applied / max(time.time() - t0, 1e-6)
            log(f"applied {applied} record(s) -> line {cursor['line']} "
                f"({rate:.1f}/s, {len(peers)} stable peer(s))")
        else:
            time.sleep(args.poll)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
