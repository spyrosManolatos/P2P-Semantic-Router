# Blue/Green Ring Migration — Stages 0–2

Two independent Chord rings running side by side on one Kubernetes cluster,
each on its own centroid artifact, completely unable to reach each other —
fed from one shared, replayable document log, and fronted by a single
client-facing address that can be repointed from one ring to the other without
stopping anything.

Part of the Adaptive Cluster Retraining work (README Future Work item 5).

| Stage | What it adds | Status |
|---|---|---|
| 0 | Two isolated rings, one artifact each | done |
| 1 | Entry point + cutover + rollback | done |
| 2 | Shared ingestion log and per-ring readers | done |
| 3 | Canary traffic split and discrepancy classifier | not started |
| 4 | Retrain trigger and runtime artifact adoption | not started |

---

## The migration lifecycle — the target design

> **This is the design being built toward, not what runs today.** The mechanics
> of the cycle are implemented and have been exercised end to end; the
> automation around them is not. What is and is not real is itemised
> [below](#what-of-this-is-actually-implemented). In particular, **nothing yet
> trains an artifact from the docs queue** — that whole arm is stage 4.

```
 t0    [ v0  blue ]                                        1 ring
       serving all traffic

 t1    [ v0  blue ]   [ v1 green ]        building         2 rings
       serving        no client traffic

 t2    [ v0  old  ]   [ v1  blue ]        cutover          2 rings
       still hot      serving                              <- rollback window

 t3                   [ v1  blue ]        decommission     1 ring
                      serving

 t4                   [ v1  blue ]   [ v2 green ]          2 rings
                      serving        next retrain reuses the freed space
```

**Green is built alongside blue, never in place of it.** The candidate ring is
additive. Dropping the live ring's pods and replacing them is not a blue/green
cutover — it is the hot swap this whole design exists to avoid, and here it
would be unusually costly: a new ring starts *empty* by deliberate design
(`emptyDir`, no warm start), so it cannot serve anything until the docs queue
has been fully replayed into it. At the measured ingest rate that is **5–6 hours
for the 98,104-document corpus**. Built alongside, those hours pass with blue
serving normally; done in place, they are an outage.

**Ring count is bounded at two, and never grows.** `v0`/`v1`/`v2` are artifact
versions, not accumulating infrastructure — `t3` tears the old ring down and
`t4` reuses the space. A thousand retrainings leave you with one ring named
`v1000`, not a thousand rings. What does accumulate is cost per *cycle*: double
the hardware for the duration of the window, plus a full re-ingest that scales
linearly with the corpus. That is the real ceiling on retraining frequency, and
it is why the warm-reuse design exists as an escape hatch once drift outpaces
what a full rebuild can keep up with (see [Future work](#future-work)).

Rebuild-and-flip is also the standard way trained indices are replaced, not a
quirk of this design: Elasticsearch and Solr reindex into a new index and flip an
**alias** — the direct analogue of the ExternalName cutover here — Lucene
segments are immutable and rebuilt rather than mutated, and retraining a
Milvus/FAISS IVF index means rebuilding and swapping it. The reason is the same
one that applies here: the trained structure determines *where things live*, so
changing it invalidates every existing placement at once.

`blue` and `green` are **roles, not names**. Blue is whichever ring is live now;
green is the candidate. They swap at `t2`. The version identifier is what stays
attached to a ring for its whole life, because a ring is *defined* by its
artifact — which is also why a ring must never adopt a new artifact in place.

### What of this is actually implemented

| Step | | |
|---|---|---|
| t1 | build a second ring on its own artifact | **done** — stage 0 |
| t1 | catch it up from the shared log | **done** — stage 2, `replay.sh` |
| t2 | cut over, and roll back | **done** — stage 1, `cutover.sh` |
| t3 | decommission | **done** — `kubectl delete -k` |
| t2 | *gate* the cutover on a quality comparison | **not implemented** — stage 3 |
| — | train the artifact **from the docs queue** | **not implemented** — stage 4 |
| — | publish and pin artifacts without an image rebuild | **not implemented** — stage 4 |
| — | trigger a retrain from observed drift | **not implemented** — stage 4 |

So the cycle's mechanics work and have been exercised; what is missing is
everything that would make it run *by itself*. Today a human decides when to
retrain, runs `train_centroids_bigk.py` by hand against a static corpus file on
the host, rebuilds the image, and decides when to cut over. `cutover.sh` will
happily switch to a ring that is caught up but *worse*, because nothing measures
quality yet.

---

## The isolation mechanism

Both rings are the **same manifests** applied to **two namespaces**. The Service
is named `dht` in both. The StatefulSet is named `dht` in both. That is
deliberate, and it is what produces isolation:

- A Service selector can only match pods in its **own namespace**, so the `dht`
  Service in `dht-v0` publishes v0's pod IPs and nothing else. There is no
  selector that could accidentally span both rings.
- A pod resolves the short name `dht-0.dht` through its **own namespace's**
  DNS search domain. The same string means a different pod in each ring. A v0
  node asking for `dht-0.dht` can only ever be answered with a v0 address.

So neither ring can *name* a peer in the other. `find_successor` has no route
to cross, regardless of whether the network would allow the packet.

`networkpolicy.yaml` states this as policy too, but note the honest caveat in
its comments: Docker Desktop's CNI ships no NetworkPolicy controller, so there
the object is stored but **not enforced**. DNS scoping is the mechanism that
actually holds on every cluster.

## What is held constant, and what is varied

|  | Across the two rings |
|---|---|
| Document set | **Identical** — both rings consume the same shared log (stage 2) |
| Node positions | **Identical** — each pod announces `dht-<ordinal>.dht`, the same string in either namespace, so SHA-1 places the vnodes at the same points on the Chord circle |
| Artifact | **Different** — the one and only variable |
| Document positions | **Different, necessarily** — see below |

Document placement is *not* constant and cannot be. A document's ring position
is `get_cluster_hash(cluster_id) = cluster_id * (2^m // k)`, and the cluster ID
comes from the artifact. A different artifact assigns the same document a
different cluster, so it lands elsewhere on the circle and is owned by a
different node.

That relocation is the effect under test, not a confound. It is the reason
retraining cannot be a hot swap and needs a second ring at all: a new artifact
changes the cluster IDs *and* their dendrogram leaf ordering, so every
document's placement moves at once.

## The two artifacts

These model an actual **retraining event**, not two unrelated models. v0 is the
artifact you fit on the data you had at time T0; the corpus then doubled, and v1
is what you get by retraining on *everything* — old documents and new — which is
what retraining actually means. v1's training input is therefore a strict
**superset** of v0's.

| | trained on | k |
|---|---|---|
| `centroids_v0.json` | the first half, 49,052 docs — Development, Business, IT & Software, Finance & Accounting, Office Productivity, part of Personal Development | 550 |
| `centroids_v1.json` | **the full corpus, 98,104 docs** — the above plus Teaching & Academics, Design, Health & Fitness, Lifestyle, Marketing, Music, Photography & Video | 550 |

k is held at 550 for both, so the difference between the artifacts is the *data*,
not the granularity.

The v0 artifact has genuinely never seen seven of the corpus's categories — the
corpus file is grouped by category, so taking the head produces real topical
gaps rather than a statistically identical sample. Their vocabularies overlap at
**Jaccard 0.665**, and the 201 terms v1 adds are exactly the drift: `ableton`,
`adobe`, `autocad`, `animation`, `anatomy`, `advertising`, `affiliate`,
`arabic`, `academics`.

To regenerate (all three files are gitignored):

```bash
python3 src/scripts/split_corpus.py            # writes the historical half

python3 src/ml/train_centroids_bigk.py --k 550 \
    --data data/raw/corpus_v0.json \
    --out  data/models/centroids_v0.json

python3 src/ml/train_centroids_bigk.py --k 550 \
    --data data/raw/normalized_kaggle_courses.json \
    --out  data/models/centroids_v1.json       # ~2 min, the whole corpus
```

The artifacts are baked into the image (`Dockerfile: COPY data/ /app/data/`),
so **rebuild after retraining**.

---

## Deploy

```bash
# 1. Build the image (must contain both centroids_v*.json)
docker build -t p2p-semantic-router:latest -f containerized_environment/Dockerfile .

# 2. The two ring-independent pieces, applied once each
kubectl apply -k k8s_bluegreen/queue        # the shared ingestion log
kubectl apply -k k8s_bluegreen/entrypoint   # the client-facing alias

# 3. Bring up both rings (each pulls in its own reader)
kubectl apply -k k8s_bluegreen/overlays/v0
kubectl apply -k k8s_bluegreen/overlays/v1

# 4. Watch them converge (OrderedReady + finger-stability probe: ~4 min total)
kubectl get pods -n dht-v0 -w
kubectl get pods -n dht-v1 -w
```

Each ring is 3 pods x 5 vnodes = 15 Chord identities. Adjust `NUM_VNODES` in
`base/statefulset.yaml` or `replicas` to scale.

## Stage 1 — the entry point

```bash
kubectl apply -k k8s_bluegreen/entrypoint
```

Clients hold exactly one name:

```
dht-active.dht-gateway.svc.cluster.local
```

It is an **ExternalName Service** — pure DNS. Kubernetes publishes a CNAME to a
ring's headless Service, which itself resolves to every ready pod in that ring:

```
dht-active.dht-gateway ──CNAME──> dht.dht-v0.svc.cluster.local
                                       └── A 10.1.0.82   (dht-0)
                                       └── A 10.1.0.84   (dht-1)
                                       └── A 10.1.0.86   (dht-2)
```

Two properties follow, and both matter for A1:

- **No membership is held.** The object names one Service, never a peer list.
  Pods joining, leaving or rescheduling are reflected by the ring's own headless
  Service with no change here. There is no central directory to keep in sync.
- **Nothing sits on the request path.** An ExternalName Service proxies nothing:
  the client's own resolver returns pod IPs and the client connects straight to
  a peer. No component sees, load balances or routes any query, so this cannot
  become the central routing authority A1 forbids.

### Cutover and rollback

```bash
./k8s_bluegreen/cutover.sh        # show which ring is live
./k8s_bluegreen/cutover.sh v1     # make v1 live
./k8s_bluegreen/cutover.sh v0     # roll back
```

The whole cutover is one field on one Service. `cutover.sh` refuses to switch to
a ring with no ready endpoints — a crude stand-in for the canary gate that
stage 3 replaces with a real regression check on live traffic.

Rollback is the same command with the other argument, because the retired ring
is never torn down or drained; it simply stops receiving *new* client traffic.

**Honest cost of the DNS approach:** propagation is bounded by TTL and
client-side caching, not immediate. CoreDNS defaults to a 30s TTL, and some
runtimes (notably the JVM) cache far longer. A client holding an open connection
keeps talking to the old ring until it re-resolves. That is the price of having
no application component in the request path.

## Stage 2 — the shared ingestion log

Stage 0 gave two rings that differ only in their artifact. That is only true if
they are fed the same documents, which is what stage 2 supplies: one
append-only log, outside both rings, that each ring consumes independently at
its own pace.

```
              publish.sh  ──►  dht-queue (producer, throwaway pod)
                                     │ append + fsync
                                     ▼
                  /var/lib/dht-bluegreen/queue/courses.jsonl
                       one record per line, immutable, replayable
                    ┌────────────────┴────────────────┐
                 read-only                        read-only
                    │                                 │
            reader (dht-v0)                   reader (dht-v1)
            cursor: cursors/v0                cursor: cursors/v1
                    │ put_course("dht:5000")          │ put_course("dht:5000")
                    ▼                                 ▼
                 ring v0                           ring v1
              artifact v0                       artifact v1
```

**One log, two cursors.** The log is shared because that is the controlled part
of the experiment; the *position* in it is not. Each reader mounts the log
`readOnly` and gets its own per-ring cursor directory, so a ring can consume the
stream but can neither write to it nor see the other ring's progress. Only the
producer, in a third namespace that belongs to neither ring, can append.

Three properties of the reader are deliberate, and all three are the same
argument that justifies the stage-1 entry point:

- **It holds no membership.** Its endpoint is one name — `dht:5000`, its own
  ring's headless Service. The ring publishes its own members; the reader takes
  whatever DNS returns. There is no peer list anywhere in it.
- **It lives inside the ring's namespace,** so that bare name resolves through
  that namespace's search domain. A v0 reader can physically only reach v0 pods,
  by exactly the mechanism that keeps the rings from naming each other. It is a
  client of one ring and cannot become a bridge between them.
- **It does not compute placement.** It calls `put_course` as-is and lets the
  contacted node vectorize, choose the cluster and route. Placement is a
  property of the ring's artifact — the thing under test — so computing it
  reader-side would silently make both rings agree.

### Delivery semantics

The cursor is committed *after* a record is applied, so a crash re-delivers at
most one record. Redelivery is harmless: `store_replica` keys storage by
`course_id`, so re-applying a record overwrites it in place. At-least-once
delivery into an idempotent sink is exactly-once in effect, with no transaction
and no dedup table. If no peer accepts a record, the reader stops rather than
skipping it — so the invariant *"every record before the cursor is in the ring"*
holds, which is what makes the cursor a meaningful readiness signal in stage 3.

A reader also refuses to route through a peer whose finger table has not
converged. The headless Service sets `publishNotReadyAddresses`, so DNS returns
joining pods too, and a PUT routed through an unconverged node can land on the
wrong successor. The reader applies the same `is_finger_stable()` predicate the
readiness probe uses before trusting a peer.

### Publishing and replaying

```bash
./k8s_bluegreen/publish.sh          # append the next 500 courses
./k8s_bluegreen/publish.sh 2000     # append the next 2000
./k8s_bluegreen/queue-status.sh     # where each ring is in the log
./k8s_bluegreen/replay.sh v1        # rewind v1 to 0 and re-consume
./k8s_bluegreen/replay.sh v1 300    # ... or from record 300
```

`publish.sh` continues from the end of the log by default, so repeated runs walk
the corpus forward. That is how "new data arrives mid-migration" is driven.

`replay.sh` is what makes blue/green possible at all: a green ring is built
empty and has to catch up on everything published so far, while blue stays live
on the *same* log. Rewinding a cursor is the whole operation — no data is copied
between rings, and the blue ring is never touched. It scales the reader to zero
first, because a running reader holds its position in memory and would commit
its stale offset back over the rewound one.

### Observed

500 records published in two batches, both rings converging independently:

```
$ ./publish.sh 300 ; ./publish.sh 200
$ ./queue-status.sh
records    : 500  (0.23 MB)

ring     cursor  applied  failed     lag  updated
v0          500      500       0       0  2026-08-15T06:16:28+00:00
v1          500      500       0       0  2026-08-15T06:16:27+00:00
```

Then a full replay of v1 from offset 0, with v0 untouched throughout —
`applied` climbing to 1000 against a 500-record log is the redelivery, and the
ring absorbing it without duplication is the idempotence claim above:

```
$ ./replay.sh v1
ring v1: cursor 500 -> 0 (byte 0)
$ ./queue-status.sh
ring     cursor  applied  failed     lag
v0          500      500       0       0     <- never interrupted
v1          500     1000       0       0     <- replayed the whole log
```

Cursors survive a reader restart, which is what keeps a rolled pod from
re-ingesting the whole log — after `kubectl rollout restart` both readers came
back at their committed position and only picked up what was new:

```
[06:26:58] resuming at line 500 (byte 225538), 500 applied so far
[06:27:52] applied 100 record(s) -> line 600
```

### The whole retraining cycle, end to end

Everything above composes into the flow the design is for. Retrain the artifact
on the grown corpus, rebuild the green ring on it, replay the log into it — with
the blue ring live and untouched from start to finish:

```bash
python3 src/ml/train_centroids_bigk.py --k 550 \
    --data data/raw/normalized_kaggle_courses.json \
    --out  data/models/centroids_v1.json              # ~2 min

docker build -t p2p-semantic-router:latest -f containerized_environment/Dockerfile .
kubectl delete -k k8s_bluegreen/overlays/v1           # green torn down
kubectl apply  -k k8s_bluegreen/overlays/v1           # green rebuilt, empty
./k8s_bluegreen/replay.sh v1 0                        # green catches up
./k8s_bluegreen/placement.sh 15                       # what changed
```

**A rebuilt ring inherits a stale cursor, and the replay is mandatory.** The
cursor lives on the node's filesystem and survives `kubectl delete -k`; the
ring's storage is `emptyDir` and does not. So the new reader came up announcing
`resuming at line 600` against a completely empty ring — caught up by its own
bookkeeping, holding nothing. Nothing detects this automatically: durability of
the cursor and durability of the ring are independent, and rebuilding one
without rewinding the other silently produces an empty ring that reports zero
lag. Until stage 4 automates the rollout, `replay.sh <ring> 0` is a required
step of the rebuild, not an optional one.

The `applied` counter is the audit trail — 1700 applications against a
600-record log is v0's original pass plus two full v1 replays:

```
ring     cursor  applied  failed     lag
v0          600      600       0       0     <- never interrupted, never rebuilt
v1          600     1700       0       0     <- rebuilt on the retrained artifact
```

### The payoff: placement actually diverges

```bash
./k8s_bluegreen/placement.sh 10
```

Same document, same log, same node positions — different owner:

```
course_id    v0 cluster v0 owner       v1 cluster v1 owner       same?
567828               92 dht-1.dht:5000        433 dht-2.dht:5004 NO
1565838             202 dht-2.dht:5001        502 dht-1.dht:5003 NO
1362070              82 dht-0.dht:5000         21 dht-0.dht:5004 NO
756150               38 dht-2.dht:5002         24 dht-2.dht:5002 yes
354176              387 dht-2.dht:5004        106 dht-1.dht:5000 NO
...
14/15 document(s) are owned by a DIFFERENT node in v1 than in v0.
```

This is the "what is varied" table above, made checkable, and it is the concrete
reason retraining cannot be an in-place artifact swap: a new artifact moves
essentially the whole corpus at once, so there is no window in which one ring is
consistent with both mappings.

**The superset is what makes this result strong.** v1 was trained on everything
v0 was trained on, plus the same amount again — it is not a rival model fit on
foreign data, it is the *same corpus retrained after it grew*, which is the
mildest realistic retraining event. And still almost every document moves.
The reason is that nothing in the pipeline preserves label identity across runs:
MiniBatchKMeans cold-starts, and the optimal-leaf-ordering step then relabels
every centroid by its position in a freshly built dendrogram. Cluster IDs are an
artifact of one training run and carry no meaning into the next. Retraining the
identical corpus with a different seed would relocate documents too.

Note that "same owner" is not "same cluster": course 756150 stayed on
`dht-2.dht:5002` while moving from cluster 38 to 24. Two different ring positions
can fall inside one node's arc — with 15 vnodes that happens by chance about
1 time in 15, which is roughly the 1/15 observed here. Every document changed
cluster; 14 of 15 also changed owner. That distinction matters for the warm-reuse
design in §9a, whose join-and-skip optimization needs documents that do not move
at all — coincidental co-location does not qualify, since the record still has to
be re-stored under a new cluster key.

`placement.sh` reads the routing decision through
`get_similar_courses(..., return_trace=True)`; the query path itself — canary
split, recall comparison — is stage 3.

### Honest limits

- **`hostPath` is a single-node convenience.** The log is a directory on the
  Docker Desktop node, which is why both namespaces can share it. On a real
  multi-node cluster it becomes a ReadWriteMany volume, and in production a
  partitioned log (Kafka, Pulsar) — at which point the cursor becomes a consumer
  group offset and this design is unchanged in shape.
- **One reader per ring, by construction.** `replicas: 1` with
  `strategy: Recreate`; two readers would race on the cursor file. Throughput is
  therefore a single stream (~6–10 records/s here, bounded by TF-IDF
  vectorization inside `put_course`, not by the log). Partitioning the log and
  running a reader per partition is the obvious scale-out and is not needed for
  the demo.
- **The log is never truncated.** Nothing here implements retention. A reader
  that finds the log *shorter* than its cursor treats it as a reset and replays
  from 0, which is safe but is not a retention policy.

## Verify

`kubectl exec` is broken on some Docker Desktop clusters (CRI HTTP/HTTPS
mismatch), so these use throwaway pods and `kubectl logs` instead.

```bash
# Every ring pod and IP, both rings at once
kubectl get pods -A -l app=dht -o wide

# What a client actually sees through the entry point
./k8s_bluegreen/probe.sh

# Ingest: where each ring is in the shared log, and how each one placed the docs
./k8s_bluegreen/queue-status.sh
./k8s_bluegreen/placement.sh 10
kubectl logs -n dht-v0 deploy/reader --tail=5
kubectl logs -n dht-v1 deploy/reader --tail=5

# Each ring loaded its own artifact
kubectl logs -n dht-v0 dht-0 | grep "Loaded artifact"
kubectl logs -n dht-v1 dht-0 | grep "Loaded artifact"

# Which artifact each ring is configured for
kubectl get cm dht-config -n dht-v0 -o jsonpath='{.data.config\.prod\.yaml}' | grep kaggle_dataset_path
kubectl get cm dht-config -n dht-v1 -o jsonpath='{.data.config\.prod\.yaml}' | grep kaggle_dataset_path
```

Observed on a 3-pod x 5-vnode deployment — the same name, before and after
`cutover.sh v1`:

```
before   resolved to : 10.1.0.82, 10.1.0.84, 10.1.0.86     (ring v0)
after    resolved to : 10.1.0.83, 10.1.0.85, 10.1.0.87     (ring v1)
```

with both rings reporting `finger_stable=True` throughout — the retired ring
keeps serving, which is what makes rollback free.

## Tear down

```bash
kubectl delete -k k8s_bluegreen/overlays/v0
kubectl delete -k k8s_bluegreen/overlays/v1
kubectl delete -k k8s_bluegreen/entrypoint
kubectl delete -k k8s_bluegreen/queue      # namespace only; the log outlives it
```

Ring pods use `emptyDir`, not PersistentVolumeClaims — unlike the single-ring
deployment in [`k8s/`](../k8s/). Shard warm-start would replay placements
computed under a *previous* artifact, which is exactly the stale-placement
problem this work exists to solve; each ring builds cleanly from its own
artifact instead. **No ring state survives teardown.**

The shared log does, deliberately: it lives at `/var/lib/dht-bluegreen` on the
node, outside every namespace, so a rebuilt ring can replay a history that
predates it. Deleting the `dht-queue` namespace removes the producer's home, not
the data. To discard it as well:

```bash
docker run --rm --privileged --pid=host alpine \
  nsenter -t 1 -m -- rm -rf /var/lib/dht-bluegreen
```

---

## Future work

Roughly in the order it would be built. Everything here is unimplemented.

### Stage 3 — gate the cutover on quality

`cutover.sh` currently checks only that the target ring has ready endpoints. It
will happily promote a ring that is caught up but *worse*. What it should
consume:

- **Ingest completeness** — the cursor lag from `queue-status.sh`, plus a check
  that the ring has ingested at least as far as its artifact was trained
  through. A green ring that is behind the log is not a candidate.
- **Canary traffic split** — mirror a fraction of real queries to both rings and
  compare. Both rings hold the same documents, so a divergence is attributable
  to the mapping.
- **A discrepancy classifier** — not every disagreement is a regression. The
  interesting split is *green found something blue missed* (the mapping
  improved) versus *green missed something blue found* (it regressed). Only the
  second should block a cutover.

### Stage 4 — make retraining a pipeline instead of a chore

The artifact is currently baked into the image (`COPY data/ /app/data/`), so
changing it means a rebuild. The target:

- **A topology builder** — a consumer of the docs queue like any reader, but one
  that trains an artifact instead of ingesting. This is what closes the loop:
  the log is replayable from offset 0, so it is exactly the right training
  input, and it records precisely what was ingested. Nothing reads it for
  training today.
- **An artifact store, not a queue** — artifacts are 4 MB blobs (≈70 MB at
  k=4096), so a JSONL log is the wrong container. A versioned directory plus a
  small manifest (`{version, k, trained_through_offset, sha256, created}`).
  `trained_through_offset` is what ties the two together and gives stage 3 its
  readiness question.
- **Rings pin an artifact version; they never follow the head.** A ring is
  defined by its artifact for its whole life. If rings subscribed to "latest",
  the artifact store would become a hot-swap channel and the blue/green premise
  would collapse.
- **Reader-side vectorization**, so only one component needs a new artifact to
  begin ingesting under it rather than N nodes atomically — and because TF-IDF
  inside `put_course` is the ingest bottleneck. This needs a guard: nodes still
  vectorize *queries*, and query-time similarity is a dot product between the
  node's query vector and the reader's stored vector. Artifact skew between
  reader and node yields a plausible number computed across two different
  feature spaces — silently wrong ranking, no error anywhere. Records must carry
  the artifact version and nodes must reject a mismatch loudly.
- **A drift trigger** — retraining should be driven by the stage-3 discrepancy
  signal, not by a schedule.

### Scaling and hygiene

- **Parameterize the ring version.** `overlays/v0`, `overlays/v1` and
  `cutover.sh`'s `case $TARGET in v0|v1)` hardcode exactly two versions. That is
  fine for one migration and wrong for a cycle — the second retrain has nowhere
  to go. Versions should be a parameter, with the freed namespace reused.
- **Parallelize the replay.** The 2–10 records/s ceiling is single-stream TF-IDF,
  not the log. Partitioning the docs queue and running a reader per partition is
  the cheapest large win available and needs none of the harder work below. This
  directly shortens the 5–6 hour rebuild that bounds retraining frequency.
- **Automate the post-rebuild replay.** A rebuilt ring inherits a stale cursor —
  the cursor is durable, the ring's `emptyDir` is not — so it reports zero lag
  while holding nothing. `replay.sh <ring> 0` is currently a manual required
  step of any rebuild.
- **Multi-node storage.** The log is a `hostPath`, which is why both namespaces
  can share it on a single node. A real cluster needs a ReadWriteMany volume,
  and production a partitioned log (Kafka, Pulsar) — at which point the cursor
  becomes a consumer-group offset and this design is unchanged in shape.
- **Log retention.** Nothing truncates the log. A reader that finds it shorter
  than its cursor treats that as a reset and replays from 0, which is safe but
  is not a retention policy.

### Warm reuse — the escape hatch, and why it is last

Rebuilding the whole ring per retrain is bounded by a full re-ingest, which
scales linearly with the corpus. If drift ever outpaces that, the alternative is
to keep the ring and move only the documents whose cluster actually changed.
That needs four things this codebase does not have:

1. **Label stability across training runs.** MiniBatchKMeans cold-starts and the
   optimal-leaf-ordering step then relabels every centroid, so cluster IDs carry
   no meaning between runs. Measured here: on a retrain whose training input was
   a strict *superset* of the previous one, 15/15 sampled documents changed
   cluster. Warm-starting or explicitly matching new centroids to old is the
   prerequisite.
2. **A delete primitive.** `store_local` only adds; relocation needs removal
   plus replica propagation.
3. **A `course_id → cluster_id` index.** Storage is cluster-first, so "did this
   document move?" has nothing cheap to answer it.
4. **Runtime artifact reload.** `_load_centroids()` runs once in `__init__` and
   `_ARTIFACT_CACHE` is keyed by path, so an in-place file swap is ignored.
   (Under version pinning this is a feature rather than a bug — different
   versions live at different paths — which is why it only becomes a blocker
   here.)

Item 1 is the gate: without label stability the other three buy nothing, because
essentially every document relocates anyway — at which point a fresh ring is
both simpler and safer, since it never has a half-migrated state. Stages 0–4
deliberately need none of these, which is why they come first.
