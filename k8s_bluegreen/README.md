# Blue/Green Ring Migration — Stages 0–1

Two independent Chord rings running side by side on one Kubernetes cluster,
each on its own centroid artifact, completely unable to reach each other —
plus a single client-facing address that can be repointed from one to the other
without stopping anything.

Part of the Adaptive Cluster Retraining work (README Future Work item 5).

| Stage | What it adds | Status |
|---|---|---|
| 0 | Two isolated rings, one artifact each | done |
| 1 | Entry point + cutover + rollback | done |
| 2 | Shared ingestion queue and per-ring readers | not started |
| 3 | Canary traffic split and discrepancy classifier | not started |
| 4 | Retrain trigger and runtime artifact adoption | not started |

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
| Document set | **Identical** — both rings ingest the same full corpus |
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

Trained on **disjoint halves** of the corpus, so their TF-IDF vocabularies are
independently fit — they share roughly 40% of their terms. Training both on the
same corpus would only produce label-permutation noise, not the genuine
geometric disagreement that models a drifted mapping.

| | trained on | categories |
|---|---|---|
| `centroids_v0.json` | first half (49,052 docs) | Development, Business, IT & Software, Personal Development, Finance & Accounting, Office Productivity |
| `centroids_v1.json` | second half (49,052 docs) | Teaching & Academics, Design, Health & Fitness, Lifestyle, Marketing, Music, Photography & Video, Personal Development |

To regenerate (all four files are gitignored):

```bash
python3 src/scripts/split_corpus.py
python3 src/ml/train_centroids_bigk.py --k 550 \
    --data data/raw/corpus_v0.json --out data/models/centroids_v0.json
python3 src/ml/train_centroids_bigk.py --k 550 \
    --data data/raw/corpus_v1.json --out data/models/centroids_v1.json
```

The artifacts are baked into the image (`Dockerfile: COPY data/ /app/data/`),
so **rebuild after retraining**.

---

## Deploy

```bash
# 1. Build the image (must contain both centroids_v*.json)
docker build -t p2p-semantic-router:latest -f containerized_environment/Dockerfile .

# 2. Bring up both rings
kubectl apply -k k8s_bluegreen/overlays/v0
kubectl apply -k k8s_bluegreen/overlays/v1

# 3. Watch them converge (OrderedReady + finger-stability probe: ~1-2 min each)
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

## Verify

`kubectl exec` is broken on some Docker Desktop clusters (CRI HTTP/HTTPS
mismatch), so these use throwaway pods and `kubectl logs` instead.

```bash
# Every ring pod and IP, both rings at once
kubectl get pods -A -l app=dht -o wide

# What a client actually sees through the entry point
./k8s_bluegreen/probe.sh

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
```

Pods use `emptyDir`, not PersistentVolumeClaims — unlike the single-ring
deployment in [`k8s/`](../k8s/). Shard warm-start would replay placements
computed under a *previous* artifact, which is exactly the stale-placement
problem this work exists to solve; each ring builds cleanly from its own
artifact instead. Nothing survives teardown.

---

## What is still missing

- No shared ingestion queue or per-ring reader — the rings are empty (stage 2)
- No canary traffic split or discrepancy classifier; `cutover.sh` gates only on
  ready endpoints, not on query quality (stage 3)
- No retraining trigger and no runtime artifact adoption — an artifact change
  still requires an image rebuild and a fresh ring (stage 4)

Notably, stages 0–4 need neither a delete primitive, a per-document cluster
index, nor label-stable retraining, because each new ring is built fresh rather
than reconciled in place. Only the warm-reuse design (§9a) needs those.
