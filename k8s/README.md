# Kubernetes Deployment — P2P Semantic Router

Deploys the semantic-router ring on Kubernetes: **5 pods × 20 virtual nodes = 100
Chord identities**, with stable identities (StatefulSet), Chord-aware readiness
gating, and warm-start shard persistence. This is a *deployment demonstration*
(§5.x of the thesis) — all measured results in Chapter 6 come from the Docker
Compose environments and are unaffected by anything here.

## What each file is

| File | Object | Role |
|---|---|---|
| `00-namespace.yaml` | Namespace | isolates the experiment; one delete cleans everything |
| `10-configmap.yaml` | ConfigMap | runtime config, editable without image rebuild |
| `20-service.yaml` | headless Service | per-pod DNS (`dht-3.dht`) → **stable ring identity** |
| `30-statefulset.yaml` | StatefulSet | the ring: 5 pods × 20 vnodes, readiness probe, per-pod volume |
| `40-benchmark-job.yaml` | Job | the evaluation harness, run-to-completion, in-cluster |
| `50-retrain-job.yaml` | Job (stub) | centroid retraining in-cluster (first step of blue/green re-index) |

Key design points, in one line each:
- **StatefulSet, not Deployment**: ring position = SHA-1(address); identities must be stable.
- **Readiness = `is_finger_stable()`**: a pod receives no traffic until every vnode's finger table has converged (the benchmark's convergence gate, promoted to infrastructure).
- **volumeClaimTemplates**: each pod owns its shard volume → a respawned pod **warm-starts** with its data (append-only corpus ⇒ a warm shard is never wrong, only possibly incomplete; join migration reconciles the delta).
- **Pods are not semantically aligned**: SHA-1 scatters each pod's 20 vnodes around the ring, so no pod holds a contiguous semantic arc (measured for these addresses: 23/100 adjacent pairs co-hosted, ≈ the N(V−1)/(N−1) prediction).

## 0. Prerequisites (once)

1. Docker Desktop → Settings → **Kubernetes** → *Enable Kubernetes* → Apply & Restart.
2. Verify:
   ```bash
   kubectl get nodes        # one node, e.g. "docker-desktop   Ready"
   ```

## 1. Build the image (bakes in current code)

```bash
cd /home/spman/ceid/Thesis
docker build -f containerized_environment/Dockerfile -t p2p-semantic-router:latest .
```
Docker Desktop's Kubernetes shares the local Docker daemon, so the image is
immediately visible in-cluster (`imagePullPolicy: Never`).

## 2. Deploy the ring

```bash
kubectl apply -f k8s/00-namespace.yaml -f k8s/10-configmap.yaml -f k8s/20-service.yaml -f k8s/30-statefulset.yaml
kubectl -n dht get pods -w        # watch; Ctrl+C to stop watching
```
Pods appear **in order** (`dht-0` → `dht-4`) and each turns `READY 1/1` only when
all 20 of its vnodes report converged finger tables. First full readiness can
take a few minutes — that is the probe doing its job, not a hang. Inspect a
pod's ring identities:

```bash
kubectl -n dht logs dht-0 | head -30      # 20 vnodes joining, scattered ring IDs
```

## 3. Demo A — elasticity (declarative scaling)

```bash
kubectl -n dht scale statefulset dht --replicas=7   # +40 ring identities
kubectl -n dht get pods -w
```
`dht-5`/`dht-6` boot, their vnodes hash to ring positions, join via `dht-0`,
claim their arcs (watch the migration lines in `kubectl -n dht logs dht-5`),
and go Ready only when converged. Scale back down afterwards:

```bash
kubectl -n dht scale statefulset dht --replicas=5
```

## 4. Demo B — self-healing with warm start (the showcase)

Inject some data first (or run the benchmark Job once, which bulk-loads), then:

```bash
kubectl -n dht delete pod dht-3        # 20 correlated vnode deaths
kubectl -n dht get pods -w
```
Watch the three healing layers:
1. **Ring (seconds):** survivors' logs show stabilization routing around the
   dead identities; replicas serve their arcs.
2. **K8s (tens of seconds):** the reconciliation loop respawns `dht-3` — same
   name → same DNS → same 20 ring positions.
3. **Warm start:** the new pod's log shows
   `WARM START: loaded shard from persistent volume (N courses ...)` for each
   vnode — data present with near-zero network traffic — then readiness gates
   it until fingers converge.

## 5. Benchmark as a Job (optional)

```bash
kubectl apply -f k8s/40-benchmark-job.yaml
kubectl -n dht logs -f job/dht-benchmark
```
Runs the standard harness (convergence gates → bulk load → hops/recall sweep)
against the live ring. Note: pod DNS names differ from the Compose hostnames,
so SHA-1 ring positions shift — hop averages match the Compose results in
shape/distribution, not to the second decimal. To re-run, delete the Job first
(`kubectl -n dht delete job dht-benchmark`).

## 6. Cleanup

```bash
kubectl delete namespace dht                          # everything except...
kubectl -n dht delete pvc --all 2>/dev/null || true   # ...PVCs, if you want the shards gone too
```
(PersistentVolumeClaims outlive the namespace deletion by design — that's the
persistence guarantee working. Delete them explicitly to discard the shards.)

## Troubleshooting

- `ErrImageNeverPull` → the image isn't in the local daemon; redo step 1.
- Pod `Running` but never `READY` → fingers not converged yet; check
  `kubectl -n dht logs <pod>` for join/stabilization errors. On a loaded laptop
  the 100-identity ring can take several minutes.
- Ring misbehaves after experiments → `kubectl -n dht rollout restart statefulset dht`
  (fresh processes, same identities, warm shards).
