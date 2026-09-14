#!/usr/bin/env bash
# Stage-2 verification: show that the SAME document lands in a different cluster,
# on a different node, in each ring -- from one shared log, because the artifact
# is the only thing that differs.
#
#   ./placement.sh            # sample 8 documents from the head of the log
#   ./placement.sh 25         # sample 25
#
# This is the claim README.md makes in "what is held constant, and what is
# varied", made checkable. Node positions are identical across the rings (same
# address strings -> same SHA-1), so any difference in the owning node is caused
# by the document's cluster id, and the cluster id comes from the artifact.
#
# It uses get_similar_courses(..., return_trace=True) purely as a read-only probe
# of the routing decision; the query PATH itself (canary split, recall
# comparison) is stage 3.
set -euo pipefail

NS=dht-gateway
IMAGE=p2p-semantic-router:latest
SAMPLE="${1:-8}"
POD="placement-$$"

read -r -d '' PY <<PYEOF || true
import json, socket, xmlrpc.client
socket.setdefaulttimeout(20)

SAMPLE = ${SAMPLE}
RINGS = ["v0", "v1"]

with open("/queue/courses.jsonl") as f:
    docs = [json.loads(l) for _, l in zip(range(SAMPLE), f)]
if not docs:
    raise SystemExit("the shared log is empty -- run ./publish.sh first")

def peer(ring):
    """One stable peer of a ring, discovered through the ring's OWN headless
    Service. No peer list is configured here."""
    name = f"dht.dht-{ring}.svc.cluster.local"
    for ai in socket.getaddrinfo(name, 5000, socket.AF_INET, socket.SOCK_STREAM):
        ip = ai[4][0]
        try:
            p = xmlrpc.client.ServerProxy(f"http://{ip}:5000", allow_none=True)
            if p.is_finger_stable():
                return ip, p
        except Exception:
            continue
    raise SystemExit(f"no finger-stable peer in ring {ring}")

proxies = {}
for r in RINGS:
    ip, p = peer(r)
    proxies[r] = p
    print(f"ring {r}: entering at {ip}")
print()

hdr = f"{'course_id':<12} {'v0 cluster':>10} {'v0 owner':<14} {'v1 cluster':>10} {'v1 owner':<14} same?"
print(hdr)
print("-" * len(hdr))

moved = 0
for d in docs:
    row = {}
    for r in RINGS:
        _, _, trace = proxies[r].get_similar_courses(json.dumps(d), 1, False, True)
        row[r] = (trace["primary_cluster"], trace["primary_owner"])
    same = row["v0"][1] == row["v1"][1]
    moved += 0 if same else 1
    print(f"{str(d['course_id']):<12} {row['v0'][0]:>10} {row['v0'][1]:<14} "
          f"{row['v1'][0]:>10} {row['v1'][1]:<14} {'yes' if same else 'NO'}")

print()
print(f"{moved}/{len(docs)} document(s) are owned by a DIFFERENT node in v1 than in v0.")
print("Same corpus, same node positions, different artifact -- this relocation is")
print("why retraining cannot be a hot swap and needs a second ring.")
PYEOF

read -r -d '' OVERRIDES <<EOF || true
{
  "spec": {
    "restartPolicy": "Never",
    "containers": [{
      "name": "placement",
      "image": "${IMAGE}",
      "imagePullPolicy": "Never",
      "command": ["python", "-c", $(printf '%s' "$PY" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')],
      "volumeMounts": [{"name": "queue", "mountPath": "/queue", "readOnly": true}]
    }],
    "volumes": [{
      "name": "queue",
      "hostPath": {"path": "/var/lib/dht-bluegreen/queue", "type": "DirectoryOrCreate"}
    }]
  }
}
EOF

kubectl run "$POD" -n "$NS" --image="$IMAGE" --restart=Never --quiet \
  --overrides="$OVERRIDES" >/dev/null 2>&1
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" -n "$NS" --timeout=180s >/dev/null 2>&1 || true
kubectl logs -n "$NS" "$POD" 2>&1
kubectl delete pod "$POD" -n "$NS" --wait=false >/dev/null 2>&1 || true
