#!/usr/bin/env bash
# Append courses to the SHARED ingestion log. Run it as many times as you like;
# each run continues where the log left off, so this is how "new data arrives"
# is driven during a blue/green migration.
#
#   ./publish.sh              # append the next 500 courses
#   ./publish.sh 2000         # append the next 2000
#   ./publish.sh 500 0        # republish corpus[0:500] (explicit start index)
#
# Both rings' readers pick the new records up on their own, independently, each
# applying them through its own artifact. Nothing here talks to a ring.
#
# Runs as a throwaway pod because `kubectl exec` is broken on some Docker
# Desktop clusters (CRI HTTP/HTTPS mismatch); same pattern as probe.sh.
set -euo pipefail

NS=dht-queue
IMAGE=p2p-semantic-router:latest
HOSTDIR=/var/lib/dht-bluegreen/queue
COUNT="${1:-500}"
START="${2:--1}"          # -1 = continue from the end of the log
POD="publish-$$"

if ! kubectl get ns "$NS" >/dev/null 2>&1; then
  echo "error: namespace $NS is missing. Run: kubectl apply -k k8s_bluegreen/queue" >&2
  exit 1
fi

read -r -d '' OVERRIDES <<EOF || true
{
  "spec": {
    "restartPolicy": "Never",
    "containers": [{
      "name": "producer",
      "image": "${IMAGE}",
      "imagePullPolicy": "Never",
      "command": ["python", "src/scripts/queue_producer.py",
                  "--queue", "/queue/courses.jsonl",
                  "--count", "${COUNT}",
                  "--start", "${START}"],
      "volumeMounts": [{"name": "queue", "mountPath": "/queue"}]
    }],
    "volumes": [{
      "name": "queue",
      "hostPath": {"path": "${HOSTDIR}", "type": "DirectoryOrCreate"}
    }]
  }
}
EOF

kubectl run "$POD" -n "$NS" --image="$IMAGE" --restart=Never --quiet \
  --overrides="$OVERRIDES" >/dev/null 2>&1

kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" -n "$NS" --timeout=180s >/dev/null 2>&1 || true
kubectl logs -n "$NS" "$POD" 2>&1
kubectl delete pod "$POD" -n "$NS" --wait=false >/dev/null 2>&1 || true

echo
echo "Both readers will pick these up within ~2s. Watch with:"
echo "  ./k8s_bluegreen/queue-status.sh"
