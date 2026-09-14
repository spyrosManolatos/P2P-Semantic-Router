#!/usr/bin/env bash
# Blue/green cutover: repoint the entry point at the other ring.
#
#   ./cutover.sh          show which ring is live
#   ./cutover.sh v1       make v1 live
#   ./cutover.sh v0       roll back
#
# The cutover is one field on one Service. Nothing is torn down, no data moves,
# and the retired ring keeps running -- so rollback is this same command with
# the other argument.
set -euo pipefail

NS=dht-gateway
SVC=dht-active

current() {
  kubectl get svc "$SVC" -n "$NS" -o jsonpath='{.spec.externalName}' 2>/dev/null
}

if [ $# -eq 0 ]; then
  cur=$(current)
  echo "entry point : ${SVC}.${NS}.svc.cluster.local"
  echo "currently   : ${cur}"
  echo "live ring   : $(kubectl get svc "$SVC" -n "$NS" -o jsonpath='{.metadata.annotations.bluegreen\.thesis/live-ring}')"
  echo
  echo "Usage: $0 <v0|v1>"
  exit 0
fi

TARGET="$1"
case "$TARGET" in
  v0|v1) ;;
  *) echo "error: ring must be 'v0' or 'v1' (got '$TARGET')" >&2; exit 1 ;;
esac

NEW="dht.dht-${TARGET}.svc.cluster.local"
OLD=$(current)

if [ "$OLD" = "$NEW" ]; then
  echo "already live on ${TARGET} (${NEW}); nothing to do."
  exit 0
fi

# Refuse to cut over to a ring that has no ready peers. This is the crude
# stand-in for the canary gate: stage 3 replaces it with a real
# regression-signal check on live traffic.
READY=$(kubectl get endpointslice -n "dht-${TARGET}" -l kubernetes.io/service-name=dht \
          -o jsonpath='{range .items[*].endpoints[*]}{.conditions.ready}{"\n"}{end}' 2>/dev/null | grep -c true || true)
if [ "${READY:-0}" -eq 0 ]; then
  echo "refusing: ring ${TARGET} has no ready endpoints." >&2
  exit 1
fi
echo "target ring ${TARGET}: ${READY} ready endpoint(s)"

kubectl patch svc "$SVC" -n "$NS" --type=merge \
  -p "{\"spec\":{\"externalName\":\"${NEW}\"},\"metadata\":{\"annotations\":{\"bluegreen.thesis/live-ring\":\"${TARGET}\"}}}" >/dev/null

echo "cutover: ${OLD}  ->  ${NEW}"
echo
echo "NOTE: this is DNS. Clients that already resolved the name keep using the"
echo "previous ring until their cache expires (CoreDNS TTL is 30s by default)."
echo "The old ring is still running and still serving -- roll back with: $0 ${OLD#dht.dht-}" | sed 's/\.svc\.cluster\.local//'
