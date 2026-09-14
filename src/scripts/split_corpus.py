"""Produce the HISTORICAL training corpus -- the data that existed before the drift.

This exists to build two artifacts that model an actual retraining event, not two
unrelated models:

    v0 artifact  <- data/raw/corpus_v0.json          the first half of the corpus
    v1 artifact  <- normalized_kaggle_courses.json   the WHOLE corpus

That is the real shape of adaptive retraining. You fit an artifact on the data
you have at time T0. The corpus then grows -- new topics appear, existing
clusters fill unevenly -- and you retrain on EVERYTHING, old and new, not on the
increment alone. v1 is therefore a strict superset of v0's training input, which
is why this script only writes one file: the second artifact needs no special
corpus, it trains on the full one.

What is held constant between the two rings:

    * the document set -- both rings ingest the same shared log (stage 2)
    * the NODE positions -- each pod announces "dht-<ordinal>.dht", the same
      string in either namespace, so SHA-1 places the vnodes at identical
      points on the Chord circle in both rings

What is varied -- and it is the only thing varied:

    * the artifact, i.e. the centroid table + vocabulary

Note that DOCUMENT positions are NOT held constant, and cannot be. A document's
ring position is get_cluster_hash(cluster_id) = cluster_id * (2**m // k), and the
cluster ID comes from the artifact. A retrained artifact therefore assigns the
same document a different cluster, puts it at a different point on the circle,
and hands it to a different node. That relocation is precisely the effect under
test, not a confound -- it is why retraining cannot be a hot swap and needs a
second ring at all.

Why the FIRST half specifically: normalized_kaggle_courses.json is grouped by
category, so taking the head gives a corpus with genuine topical gaps rather
than a statistically identical sample of the whole. The v0 artifact has simply
never seen the categories that arrive later:

    in corpus_v0    Development, Business, IT & Software, Finance & Accounting,
                    Office Productivity, Personal Development (partial)
    added by v1     Teaching & Academics, Design, Health & Fitness, Lifestyle,
                    Marketing, Music, Photography & Video, and the rest of
                    Personal Development

So v0 is a stale mapping in the way that matters -- it has to force unseen
topics into clusters fitted for other material -- while v1 is the mapping you
would get by retraining once the corpus had doubled.

Usage:

    python3 src/scripts/split_corpus.py

    python3 src/ml/train_centroids_bigk.py --k 550 \
        --data data/raw/corpus_v0.json \
        --out  data/models/centroids_v0.json

    python3 src/ml/train_centroids_bigk.py --k 550 \
        --data data/raw/normalized_kaggle_courses.json \
        --out  data/models/centroids_v1.json

Both k values are kept at 550 on purpose: holding k fixed means the difference
between the artifacts is the DATA, not the granularity. Outputs are gitignored --
they are reproducible from here.
"""
import argparse
import collections
import json
import os


def project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def summarize(name, part):
    print(f"\n{name}: {len(part)} docs")
    for cat, n in collections.Counter(c["category"] for c in part).most_common():
        print(f"    {cat:<28} {n:5d}")


def main():
    root = project_root()
    parser = argparse.ArgumentParser(
        description="Write the historical (pre-drift) half of the corpus, used to train the v0 artifact."
    )
    parser.add_argument("--data", default=os.path.join(root, "data/raw/normalized_kaggle_courses.json"),
                        help="Full normalized corpus. Also the v1 artifact's training input, unmodified.")
    parser.add_argument("--out-v0", default=os.path.join(root, "data/raw/corpus_v0.json"))
    parser.add_argument("--fraction", type=float, default=0.5,
                        help="Head fraction of the corpus that counts as 'already existed'.")
    args = parser.parse_args()

    if not os.path.exists(args.data):
        raise SystemExit(
            f"Corpus not found: {args.data}\n"
            "Run 'python3 src/scripts/ingest_kaggle_data.py' first to produce it from the raw CSV."
        )

    with open(args.data, "r", encoding="utf-8") as f:
        docs = json.load(f)

    cut = int(len(docs) * args.fraction)
    historical = docs[:cut]

    os.makedirs(os.path.dirname(args.out_v0), exist_ok=True)
    with open(args.out_v0, "w", encoding="utf-8") as f:
        json.dump(historical, f)

    # The categories v0 never saw are the whole point: they are what a stale
    # artifact has to mis-map, and what retraining on the full corpus fixes.
    seen = {c["category"] for c in historical}
    added = collections.Counter(c["category"] for c in docs[cut:] if c["category"] not in seen)

    print(f"Full corpus     : {len(docs)} docs  ({args.data})")
    summarize("corpus_v0 (trains the v0 artifact)", historical)
    print(f"\nv1 artifact trains on the FULL corpus -- no separate file needed.")
    print(f"Growth v0 -> v1 : {len(docs) - cut} new docs (+{100 * (len(docs) - cut) / cut:.0f}%)")
    print("\nCategories the v0 artifact has never seen:")
    if not added:
        print("    (none -- the head fraction already covers every category;"
              " lower --fraction for a starker split)")
    for cat, n in added.most_common():
        print(f"    {cat:<28} {n:5d}")
    print(f"\nWrote {args.out_v0}")


if __name__ == "__main__":
    main()
