#!/usr/bin/env bash
# Rewind one ring's cursor and let its reader re-consume the shared log.
#
#   ./replay.sh v1          # replay v1 from the start of the log
#   ./replay.sh v1 300      # replay v1 from record 300
#
# This is what makes blue/green possible at all: a green ring is built empty and
# has to catch up on everything published so far, while blue stays live and
# stays caught up on the SAME log. Rewinding the cursor is the entire operation
# -- the log is immutable and shared, so no data is copied between rings and the
# blue ring is not touched.
#
# Replay is safe to run at any time. Records are keyed by course_id in
# store_replica, so re-applying one overwrites it in place; the ring converges to
# the same state whether a record is delivered once or five times.
#
# NOTE: rewinding to a byte offset requires walking the log to the requested
# record, which this does inside the pod. Only whole records are counted.
set -euo pipefail

RING="${1:-}"
FROM="${2:-0}"
NS=dht-queue
IMAGE=p2p-semantic-router:latest
ROOT=/var/lib/dht-bluegreen
POD="replay-$$"

case "$RING" in
  v0|v1) ;;
  *) echo "usage: $0 <v0|v1> [from-record]" >&2; exit 1 ;;
esac

read -r -d '' PY <<PYEOF || true
import json, os
RING, FROM = "${RING}", ${FROM}

log = "/data/queue/courses.jsonl"
cur_dir = os.path.join("/data/cursors", RING)
os.makedirs(cur_dir, exist_ok=True)
cur = os.path.join(cur_dir, "offset.json")

# Byte offset of record FROM: the reader seeks by byte, so the record index has
# to be translated by walking the log. Records are whole lines.
byte = line = 0
if FROM > 0:
    with open(log, "rb") as f:
        for raw in f:
            if line >= FROM:
                break
            byte += len(raw)
            line += 1
    if line < FROM:
        raise SystemExit(f"log holds only {line} record(s); cannot start at {FROM}")

prev = {}
if os.path.exists(cur):
    with open(cur) as f:
        prev = json.load(f)

new = {"ring": RING, "byte": byte, "line": line,
       "applied": int(prev.get("applied", 0)), "failed": int(prev.get("failed", 0))}
tmp = cur + ".tmp"
with open(tmp, "w") as f:
    json.dump(new, f, indent=2)
    f.flush()
    os.fsync(f.fileno())
os.replace(tmp, cur)

print(f"ring {RING}: cursor {prev.get('line', 0)} -> {line} (byte {byte})")
print(f"the reader will now re-apply every record from {line} onward")
PYEOF

read -r -d '' OVERRIDES <<EOF || true
{
  "spec": {
    "restartPolicy": "Never",
    "containers": [{
      "name": "replay",
      "image": "${IMAGE}",
      "imagePullPolicy": "Never",
      "command": ["python", "-c", $(printf '%s' "$PY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')],
      "volumeMounts": [{"name": "data", "mountPath": "/data"}]
    }],
    "volumes": [{
      "name": "data",
      "hostPath": {"path": "${ROOT}", "type": "DirectoryOrCreate"}
    }]
  }
}
EOF

# Stop the reader first: it holds the cursor in memory and would commit its own
# stale position over this one on its next batch.
kubectl scale deploy/reader -n "dht-${RING}" --replicas=0 >/dev/null
kubectl wait --for=delete pod -l app=reader -n "dht-${RING}" --timeout=60s >/dev/null 2>&1 || true

kubectl run "$POD" -n "$NS" --image="$IMAGE" --restart=Never --quiet \
  --overrides="$OVERRIDES" >/dev/null 2>&1
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" -n "$NS" --timeout=120s >/dev/null 2>&1 || true
kubectl logs -n "$NS" "$POD" 2>&1
kubectl delete pod "$POD" -n "$NS" --wait=false >/dev/null 2>&1 || true

kubectl scale deploy/reader -n "dht-${RING}" --replicas=1 >/dev/null
echo
echo "reader restarted. Follow it with:"
echo "  kubectl logs -n dht-${RING} deploy/reader -f"
