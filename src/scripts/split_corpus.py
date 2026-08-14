"""Split the normalized corpus into two disjoint halves -- as TRAINING INPUT ONLY.

These halves are used to train two centroid artifacts that genuinely disagree.
They are NOT what the rings store.

    training input   v0 artifact <- first half     v1 artifact <- second half
    ring contents    BOTH rings ingest the SAME full corpus

What is held constant between the two rings:

    * the document set -- both rings ingest the same full corpus
    * the NODE positions -- each pod announces "dht-<ordinal>.dht", the same
      string in either namespace, so SHA-1 places the vnodes at identical
      points on the Chord circle in both rings

What is varied -- and it is the only thing varied:

    * the artifact, i.e. the centroid table + vocabulary

Note that DOCUMENT positions are NOT held constant, and cannot be. A document's
ring position is get_cluster_hash(cluster_id) = cluster_id * (2^m // k), and the
cluster ID comes from the artifact. A different artifact therefore assigns the
same document a different cluster, puts it at a different point on the circle,
and hands it to a different node. That relocation is precisely the effect under
test, not a confound -- it is why retraining cannot be a hot swap and needs a
second ring at all.

Why disjoint halves for training: training both artifacts on the same corpus
would only produce label-permutation noise. Disjoint halves produce
independently-fit TF-IDF vocabularies and genuinely different cluster geometry
-- which is what "the corpus drifted and the mapping went stale" actually looks
like.

normalized_kaggle_courses.json is grouped by category, so a sequential split
gives a clean topical separation rather than two statistically identical samples:

    first half   Development, Business, IT & Software, Personal Development,
                 Finance & Accounting, Office Productivity
    second half  Teaching & Academics, Design, Health & Fitness, Lifestyle,
                 Marketing, Music, Photography & Video, Personal Development

Only "Personal Development" straddles the boundary. The resulting artifacts
share roughly 40% of their vocabulary terms.

Usage:

    python3 src/scripts/split_corpus.py

    python3 src/ml/train_centroids_bigk.py --k 550 \
        --data data/raw/corpus_v0.json --out data/models/centroids_v0.json
    python3 src/ml/train_centroids_bigk.py --k 550 \
        --data data/raw/corpus_v1.json --out data/models/centroids_v1.json

The rings are then loaded from the FULL corpus, not from these halves. All four
outputs are gitignored -- they are reproducible from this script.
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
        description="Split the normalized corpus into two disjoint training halves."
    )
    parser.add_argument("--data", default=os.path.join(root, "data/raw/normalized_kaggle_courses.json"),
                        help="Normalized corpus to split.")
    parser.add_argument("--out-v0", default=os.path.join(root, "data/raw/corpus_v0.json"))
    parser.add_argument("--out-v1", default=os.path.join(root, "data/raw/corpus_v1.json"))
    args = parser.parse_args()

    if not os.path.exists(args.data):
        raise SystemExit(
            f"Corpus not found: {args.data}\n"
            "Run 'python3 src/scripts/ingest_kaggle_data.py' first to produce it from the raw CSV."
        )

    with open(args.data, "r", encoding="utf-8") as f:
        docs = json.load(f)

    mid = len(docs) // 2
    first, second = docs[:mid], docs[mid:]

    # A document appearing in both halves would silently break the premise that
    # the two artifacts were trained on disjoint data.
    overlap = {c["course_id"] for c in first} & {c["course_id"] for c in second}
    if overlap:
        raise SystemExit(f"Halves are not disjoint: {len(overlap)} shared course_id(s).")

    for path, part in ((args.out_v0, first), (args.out_v1, second)):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(part, f)

    print(f"Split {len(docs)} docs from {args.data}")
    summarize("corpus_v0 (trains the v0 artifact)", first)
    summarize("corpus_v1 (trains the v1 artifact)", second)
    print(f"\nWrote {args.out_v0}\nWrote {args.out_v1}")
    print("Document ID overlap: 0 (verified)")
    print("\nReminder: both rings ingest the FULL corpus, not these halves.")


if __name__ == "__main__":
    main()
