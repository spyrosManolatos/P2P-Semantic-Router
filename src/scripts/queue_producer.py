#!/usr/bin/env python3
"""Publish courses to the shared blue/green ingestion log (stage 2).

The log is a plain append-only JSONL file: one course object per line, line
number = sequence number. It sits OUTSIDE both rings and is written by no ring
member -- it is the single source of truth that both rings independently
consume, which is what makes a blue/green comparison controlled. Same document
stream in, different artifact applied, so any divergence in placement is
attributable to the artifact alone.

Why an append-only file and not a topic in a broker: the property stage 2 needs
is replayability from an arbitrary offset (a freshly built green ring must be
able to catch up from 0 while blue stays live), and a file gives that with
nothing to operate. A broker would be the production answer; see the caveat in
k8s_bluegreen/README.md.

    python src/scripts/queue_producer.py --count 500
    python src/scripts/queue_producer.py --count 500      # appends the NEXT 500

By default --start continues from the end of the log, so repeated invocations
walk the corpus forward instead of republishing the same documents. That is how
the "new data arrives mid-migration" scenario is driven.
"""
import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_SOURCE = os.path.join(PROJECT_ROOT, "data", "raw", "normalized_kaggle_courses.json")


def count_lines(path: str) -> int:
    """Number of COMPLETE records already in the log (a trailing partial line is
    not a record and is not counted)."""
    if not os.path.exists(path):
        return 0
    n = 0
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            n += chunk.count(b"\n")
    return n


def main():
    p = argparse.ArgumentParser(description="Append courses to the shared ingestion log")
    p.add_argument("--queue", default="/queue/courses.jsonl", help="Path to the append-only log")
    p.add_argument("--source", default=DEFAULT_SOURCE, help="Normalized corpus to publish from")
    p.add_argument("--count", type=int, default=500, help="How many courses to append")
    p.add_argument("--start", type=int, default=-1,
                   help="Corpus index to start from (-1 = continue from the end of the log)")
    args = p.parse_args()

    with open(args.source) as f:
        corpus = json.load(f)

    already = count_lines(args.queue)
    start = already if args.start < 0 else args.start
    if start >= len(corpus):
        print(f"nothing to publish: log already holds {already} record(s), "
              f"corpus has {len(corpus)}")
        return
    batch = corpus[start:start + args.count]

    os.makedirs(os.path.dirname(args.queue) or ".", exist_ok=True)
    # O_APPEND makes each write land at the current end of file even with more
    # than one producer, and writing each record with a SINGLE os.write keeps
    # records whole -- a reader tailing the file never sees an interleaved line.
    fd = os.open(args.queue, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        for course in batch:
            os.write(fd, (json.dumps(course, ensure_ascii=False) + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    total = already + len(batch)
    print(f"published {len(batch)} course(s): corpus[{start}:{start + len(batch)}]")
    print(f"log       : {args.queue}")
    print(f"records   : {already} -> {total}")
    if batch:
        cats = {}
        for c in batch:
            cats[c.get("category", "?")] = cats.get(c.get("category", "?"), 0) + 1
        top = sorted(cats.items(), key=lambda kv: -kv[1])[:4]
        print(f"categories: {', '.join(f'{k} ({v})' for k, v in top)}")


if __name__ == "__main__":
    sys.exit(main())
