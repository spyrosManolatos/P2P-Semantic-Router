#!/usr/bin/env bash
# Resolve the entry point from inside the cluster and talk to whichever ring
# answers. This is what a client sees -- it knows ONE name and nothing else.
#
#   ./probe.sh
#
# Runs a throwaway pod in dht-gateway (a namespace labelled dht-client, so the
# rings' NetworkPolicy admits it), resolves dht-active, and calls each peer the
# name resolved to. Prints the pod IPs so you can see WHICH ring served it.
set -euo pipefail

NS=dht-gateway
POD="probe-$$"

read -r -d '' PY <<'PYEOF' || true
import socket, xmlrpc.client
socket.setdefaulttimeout(8)
NAME = "dht-active.dht-gateway.svc.cluster.local"
ips = sorted({ai[4][0] for ai in socket.getaddrinfo(NAME, 5000, socket.AF_INET, socket.SOCK_STREAM)})
print(f"entry point : {NAME}")
print(f"resolved to : {', '.join(ips)}")
for ip in ips:
    try:
        s = xmlrpc.client.ServerProxy(f"http://{ip}:5000", allow_none=True)
        print(f"   {ip}  finger_stable={s.is_finger_stable()}  successor={s.get_successor()}")
    except Exception as e:
        print(f"   {ip}  UNREACHABLE {type(e).__name__}: {e}")
PYEOF

kubectl run "$POD" -n "$NS" --image=p2p-semantic-router:latest --image-pull-policy=Never \
  --restart=Never --quiet --command -- python -c "$PY" >/dev/null 2>&1
kubectl wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$POD" -n "$NS" --timeout=90s >/dev/null 2>&1 || true
kubectl logs -n "$NS" "$POD" 2>&1
kubectl delete pod "$POD" -n "$NS" --wait=false >/dev/null 2>&1 || true
