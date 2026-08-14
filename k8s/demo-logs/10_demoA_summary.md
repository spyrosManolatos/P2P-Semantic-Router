# Demo A — Elasticity (Scale-Up)

## Action
kubectl -n dht scale statefulset dht --replicas=7   (5 pods/100 vnodes -> 7 pods/140 vnodes)

## Sequencing (OrderedReady)
dht-5 created immediately; dht-6 was NOT created until dht-5 reached Ready
(~9 min). dht-6 then converged in turn. Total scale-up time ~19 min for both
new pods (consistent with the ~9 min single-pod convergence observed in
Demo B, given the larger, now-140-node ring resettling twice in sequence).

New PVCs auto-created and bound: shards-dht-5, shards-dht-6 (StatefulSet
volumeClaimTemplates -- no manual provisioning).

## Data conservation (the key result)
| Pod    | Before | After | Delta |
|--------|-------:|------:|------:|
| dht-0  |     77 |    75 |    -2 |
| dht-1  |     21 |     2 |   -19 |
| dht-2  |     64 |    64 |     0 |
| dht-3  |     42 |    20 |   -22 |
| dht-4  |    110 |    75 |   -35 |
| dht-5  |      - |    37 |   +37 |
| dht-6  |      - |    41 |   +41 |
| TOTAL  |    314 |   314 |     0 |

78 courses migrated from the original 5 pods to the 2 new pods (-78 / +78
balances exactly). Zero data loss, zero duplication. dht-2 was untouched --
none of its owned ring ranges fell inside the new nodes' claimed arcs,
illustrating that migration is localized to the affected ring segment, not
a whole-ring reshuffle.

## Known anomaly (documented, not resolved)
Baseline count was 314, not the 200 originally injected. All 5 original pods
had restarted simultaneously ~15h after initial injection (RESTARTS: 1),
almost certainly from a host sleep/Docker Desktop restart -- a much more
chaotic topology event than the isolated single-pod respawn in Demo B. The
inflation is suspected to stem from that simultaneous re-convergence
(possibly a replica transiently double-counted as primary), not from this
scale-up operation. Demo A's validity is unaffected: the test is total
conservation ACROSS the scale event, which held exactly (314 -> 314)
regardless of how the pre-existing baseline arose. Flagged for follow-up
investigation, not treated as blocking.

## Follow-up observation: single-host CPU ceiling at 140 identities

Poking at the cluster ~3h after the scale-up (still 7 pods/140 identities, all
`1/1 Running`, data still exactly 314 courses -- no drift), `kubectl get
events` showed a ROLLING readiness-probe failure across all 7 pods within a
single ~4-minute window ("command timed out... after 8s" on the
`is_finger_stable()` probe script), though none of them actually restarted
(`RESTARTS` counts unchanged throughout). `docker stats` explained why: each
pod's container was running at **68-124% CPU**, i.e. host CPU under real
pressure.

This is expected, not a bug: 7 pods x 20 vnodes = 140 independent,
GIL-bound Chord identities, each running its own periodic stabilization /
fix_fingers RPC loop, all sharing ONE Docker Desktop VM's CPU. At this
density the host occasionally can't service every vnode's RPCs inside the
probe's 8-second window, so the readiness probe flaps transiently -- self-
recovering, no restarts, no data loss, but a real signal.

**Practical takeaway for the thesis**: the original 100-identity deployment
(5 pods) ran the self-healing demo (Demo B) without any such flakiness; the
flakiness only appeared after scaling to 140 identities (Demo A). This
suggests **~100-140 virtual-node identities is roughly the practical upper
bound for a single physical host** running this implementation as GIL-bound
Python processes -- not a protocol limitation (Chord/the routing algorithm
scale far beyond this), but a single-machine resource ceiling. Scaling
further requires genuinely distributing identities across multiple physical
machines (see README Future Work: "Real Network Latency & Multi-Machine
Deployment"), which would also remove the GIL-contention bottleneck as a
side effect.
