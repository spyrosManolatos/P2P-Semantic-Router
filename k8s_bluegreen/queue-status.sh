#!/usr/bin/env bash
# Where each ring is in the shared log.
#
#   ./queue-status.sh
#
# This is the stage-2 view of a blue/green migration: one log, two independent
# cursors. A green ring built from scratch starts at 0 and catches up while blue
# stays live and stays caught up -- and the lag column is what says whether the
# green ring is ready to be cut over to.
set -euo pipefail

NS=dht-queue
IMAGE=p2p-semantic-router:latest
ROOT=/var/lib/dht-bluegreen
POD="qstatus-$$"

read -r -d '' PY <<'PYEOF' || true
import json, os

ROOT = "/data"
log = os.path.join(ROOT, "queue", "courses.jsonl")

total = 0
size = 0
if os.path.exists(log):
    size = os.path.getsize(log)
    with open(log, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            total += chunk.count(b"\n")

print(f"shared log : {log.replace('/data', '/var/lib/dht-bluegreen')}")
print(f"records    : {total}  ({size/1e6:.2f} MB)")
print()
print(f"{'ring':<6} {'cursor':>8} {'applied':>8} {'failed':>7} {'lag':>7}  updated")
print("-" * 62)

cur_dir = os.path.join(ROOT, "cursors")
rings = sorted(os.listdir(cur_dir)) if os.path.isdir(cur_dir) else []
if not rings:
    print("(no cursors yet -- no reader has committed a record)")
for ring in rings:
    path = os.path.join(cur_dir, ring, "offset.json")
    if not os.path.exists(path):
        print(f"{ring:<6} {'-':>8} {'-':>8} {'-':>7} {'-':>7}  never committed")
        continue
    with open(path) as f:
        c = json.load(f)
    line = int(c.get("line", 0))
    print(f"{ring:<6} {line:>8} {int(c.get('applied', 0)):>8} "
          f"{int(c.get('failed', 0)):>7} {total - line:>7}  {c.get('updated', '?')}")
PYEOF

read -r -d '' OVERRIDES <<EOF || true
{
  "spec": {
    "restartPolicy": "Never",
    "containers": [{
      "name": "status",
      "image": "${IMAGE}",
      "imagePullPolicy": "Never",
      "command": ["python", "-c", $(printf '%s' "$PY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')],
      "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": true}]
    }],
    "volumes": [{
      "name": "data",
      "hostPath": {"path": "${ROOT}", "type": "DirectoryOrCreate"}
    }]
  }
}
EOF

kubectl run "$POD" -n "$NS" --image="$IMAGE" --restart=Never --quiet \
  --overrides="$OVERRIDES" >/dev/null 2>&1
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" -n "$NS" --timeout=120s >/dev/null 2>&1 || true
kubectl logs -n "$NS" "$POD" 2>&1
kubectl delete pod "$POD" -n "$NS" --wait=false >/dev/null 2>&1 || true
