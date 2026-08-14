"""HTTP Gateway for the P2P Semantic Router.

Exposes the DHT's similarity search over a small REST API so the network can
be queried from a browser. Every query enters the ring through a RANDOM node
(simulating an arbitrary peer receiving the request) and the response reports
which node served as the entry point, which semantic clusters the entry node
decided to probe, where those clusters and all nodes sit on the Chord ring,
and how many hops the lookup required.

Run inside the compose network (see gateway service in docker-compose.yml) or
locally against the published ports:

    DHT_NODES=localhost:8001,localhost:8002,localhost:8003,localhost:8004,localhost:8005 \
        uvicorn api.main:app --app-dir src --port 8080
"""
import json
import os
import random
import socket
import time
import xmlrpc.client
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

# Nodes that are members of the ring at startup (node-5 joins later during
# the join benchmark, so it is not part of the default pool).
DEFAULT_NODES = "bootstrap-node:5000,node-1:5000,node-2:5000,node-3:5000,node-4:5000"
NODES: List[str] = [a.strip() for a in os.environ.get("DHT_NODES", DEFAULT_NODES).split(",") if a.strip()]

RPC_TIMEOUT_SECONDS = 15
socket.setdefaulttimeout(RPC_TIMEOUT_SECONDS)

app = FastAPI(
    title="P2P Semantic Router Gateway",
    description="Query the semantic DHT from a random entry node and inspect routing hops.",
)


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1, description="Free-text similarity query, e.g. 'machine learning for beginners'")
    nprobe: int = Field(1, ge=1, le=10, description="Number of semantic clusters to probe")
    entry_node: Optional[str] = Field(None, description="Force a specific entry node (host:port). Random if omitted.")


# Transport selection: the sync stacks speak XML-RPC; the async (FastAPI/httpx)
# stacks speak JSON-RPC over POST /rpc/{method}. TRANSPORT=json switches the
# client so one gateway image serves both. JSONRPCProxy mirrors ServerProxy's
# call interface (proxy.method(*args) -> result), so nothing else changes.
TRANSPORT = os.environ.get("TRANSPORT", "xmlrpc").lower()

if TRANSPORT == "json":
    from core.json_rpc_client import JSONRPCProxy

    def _rpc(address: str) -> "JSONRPCProxy":
        return JSONRPCProxy(address, timeout=RPC_TIMEOUT_SECONDS)
else:
    def _rpc(address: str) -> xmlrpc.client.ServerProxy:
        return xmlrpc.client.ServerProxy(f"http://{address}", allow_none=True)


def _ring_pct(hash_str: str, bits: int) -> float:
    """Position of a hash on the ring as a percentage of the full circle."""
    return round(int(hash_str) / (2 ** bits) * 100, 2)


def _ring_snapshot(bits: int) -> List[Dict[str, Any]]:
    """Current ring membership with each node's position on the identifier circle."""
    ring = []
    for addr in NODES:
        try:
            info = _rpc(addr).get_info()
            ring.append({
                "address": addr,
                "node_id": info["node_id"],
                "ring_pct": _ring_pct(info["node_id"], bits),
                "status": "online",
            })
        except Exception:
            ring.append({"address": addr, "status": "offline"})
    return ring


@app.post("/query")
def query(req: QueryRequest) -> Dict[str, Any]:
    """Route a similarity query through a random ring node and explain the routing."""
    if req.entry_node:
        if req.entry_node not in NODES:
            raise HTTPException(status_code=400, detail=f"Unknown node '{req.entry_node}'. Known nodes: {NODES}")
        candidates = [req.entry_node]
    else:
        candidates = random.sample(NODES, len(NODES))

    # get_similar_courses vectorizes title+category+description, so a free-text
    # query is passed as the title of a synthetic course record.
    course_json = json.dumps({"course_title": req.query, "category": "", "description": ""})

    errors: Dict[str, str] = {}
    for entry in candidates:
        try:
            start = time.time()
            results, hops, trace = _rpc(entry).get_similar_courses(course_json, req.nprobe, True, True)
            latency_ms = (time.time() - start) * 1000
        except Exception as e:
            errors[entry] = str(e)
            continue

        bits = trace["ring_bits"]

        # Which node ended up serving each probed cluster.
        served_by: Dict[int, str] = {}
        for contact in trace["nodes_contacted"]:
            for cid in contact["clusters"]:
                served_by.setdefault(cid, contact["node"])

        # Clustered DHT: SHA-1 placement forces one Chord lookup per cluster,
        # so the trace carries a lookup (hops + path) for each. The semantic
        # router has a single lookup and this list stays empty.
        lookups = trace.get("lookups", [])
        lookup_by_cid = {lk["cluster_id"]: lk for lk in lookups}

        clusters = [
            {
                "cluster_id": cid,
                "hash": trace["cluster_hashes"][str(cid)],
                "ring_pct": _ring_pct(trace["cluster_hashes"][str(cid)], bits),
                "served_by": served_by.get(cid) or (lookup_by_cid.get(cid) or {}).get("owner"),
                "lookup_hops": (lookup_by_cid.get(cid) or {}).get("hops"),
            }
            for cid in trace["top_clusters"]
        ]

        # Finger tables of every node any lookup passed through (entry + hops),
        # compressed into ranges of fingers that point at the same node.
        path_nodes = list(trace.get("lookup_path", []))
        for lk in lookups:
            path_nodes.extend(lk["path"])
        finger_tables: Dict[str, Any] = {}
        for addr in dict.fromkeys(path_nodes):
            try:
                finger_tables[addr] = [
                    {
                        "fingers": f"{g['from_finger']}-{g['to_finger']}",
                        "start_pct": _ring_pct(g["start_hash"], bits),
                        "node": g["node"],
                    }
                    for g in _rpc(addr).get_finger_table()
                ]
            except Exception:
                finger_tables[addr] = None

        return {
            "query": req.query,
            "nprobe": req.nprobe,
            "entry_node": entry,
            "entry_node_selection": "forced" if req.entry_node else "random",
            "hops": hops,
            "latency_ms": round(latency_ms, 2),
            "routing": {
                "entry_node": trace["entry_node"],
                "entry_node_id": trace["entry_node_id"],
                "entry_node_ring_pct": _ring_pct(trace["entry_node_id"], bits),
                "clusters": clusters,
                "primary_cluster": trace["primary_cluster"],
                "primary_owner": trace["primary_owner"],
                "chord_hops_to_owner": trace["chord_hops_to_owner"],
                "lookup_path": trace.get("lookup_path", []),
                "lookups": lookups or None,
                "finger_tables": finger_tables,
                "nodes_contacted": trace["nodes_contacted"],
            },
            "ring": _ring_snapshot(bits),
            "results": [json.loads(c) for c in results],
            "unreachable_nodes": errors or None,
        }

    raise HTTPException(status_code=503, detail={"message": "No ring node reachable", "errors": errors})


@app.get("/nodes")
def nodes() -> List[Dict[str, Any]]:
    """Ring membership view: status, successor/predecessor and stored clusters per node."""
    out = []
    for addr in NODES:
        try:
            info = _rpc(addr).get_info()
            out.append({
                "address": addr,
                "status": "online",
                "node_id": info["node_id"],
                "successor": info["successor"],
                "predecessor": info["predecessor"],
                "primary_clusters": {cid: n for cid, n in info["primary_summary"].items()},
                "replica_clusters": {cid: n for cid, n in info["replica_summary"].items()},
            })
        except Exception as e:
            out.append({"address": addr, "status": "offline", "error": str(e)})
    return out


DEMO_PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__ Gateway</title>
<style>
  :root {
    color-scheme: light dark;
    --c-node: #2a78d6;      /* categorical slot 1 (blue) */
    --c-cluster: #eda100;   /* categorical slot 3 (yellow) */
    --ink-2: #52514e;
    --surface: #ffffff;
    --line: #8884;
  }
  @media (prefers-color-scheme: dark) {
    :root { --c-node: #3987e5; --c-cluster: #c98500; --ink-2: #c3c2b7; --surface: #1a1a19; }
  }
  body { font-family: system-ui, sans-serif; max-width: 980px; margin: 2rem auto; padding: 0 1rem; line-height: 1.5; }
  h1 { font-size: 1.4rem; }
  form { display: flex; gap: .5rem; flex-wrap: wrap; margin-bottom: 1rem; }
  input[type=text] { flex: 1; min-width: 240px; padding: .55rem .7rem; font-size: 1rem; border: 1px solid var(--line); border-radius: 6px; }
  select, button { padding: .55rem .7rem; font-size: 1rem; border: 1px solid var(--line); border-radius: 6px; }
  button { cursor: pointer; font-weight: 600; }
  .meta { display: flex; gap: 1.5rem; flex-wrap: wrap; margin: 1rem 0; font-size: .95rem; }
  .meta b { font-size: 1.25rem; display: block; }
  .cols { display: flex; gap: 1.5rem; flex-wrap: wrap; align-items: flex-start; }
  .cols > div { flex: 1; min-width: 320px; }
  .card { border: 1px solid var(--line); border-radius: 8px; padding: .7rem .9rem; margin: .5rem 0; }
  .card small, .muted { color: var(--ink-2); }
  .sim { float: right; font-variant-numeric: tabular-nums; color: var(--ink-2); }
  #status { color: var(--ink-2); }
  table { border-collapse: collapse; width: 100%; font-size: .9rem; }
  th, td { text-align: left; padding: .3rem .5rem; border-bottom: 1px solid var(--line); }
  th { color: var(--ink-2); font-weight: 600; }
  code { font-size: .85em; }
  .paths div { font-size: .92rem; margin: .15rem 0; }
  details { margin: .5rem 0; }
  summary { cursor: pointer; }
  .legend { display: flex; gap: 1.2rem; font-size: .85rem; color: var(--ink-2); margin: .4rem 0; }
  .legend span { display: inline-flex; align-items: center; gap: .35rem; }
  .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--c-node); display: inline-block; }
  .dot.entry { outline: 2px solid var(--c-node); outline-offset: 2px; }
  .diamond { width: 9px; height: 9px; background: var(--c-cluster); display: inline-block; transform: rotate(45deg); }
  svg text { fill: currentColor; font-family: system-ui, sans-serif; }
  svg .lbl { font-size: 11px; }
  svg .lbl2 { font-size: 10px; fill: var(--ink-2); }
</style>
</head>
<body>
<h1>__TITLE__ &mdash; Query Gateway</h1>
<p>Each query enters the Chord ring through a <b>random node</b>. The entry node vectorizes the query, decides which semantic clusters to probe, and routes to the peers owning them.</p>
<form id="f">
  <input type="text" id="q" placeholder="e.g. python for data science" required>
  <select id="nprobe">
    <option value="1">nprobe = 1</option>
    <option value="2">nprobe = 2</option>
    <option value="3">nprobe = 3</option>
  </select>
  <button>Search</button>
</form>
<div id="status"></div>
<div class="meta" id="meta" hidden>
  <span>Entry node<b id="entry"></b></span>
  <span>Clusters probed<b id="cl"></b></span>
  <span>Hops<b id="hops"></b></span>
  <span>Latency<b id="lat"></b></span>
</div>
<div class="cols">
  <div id="ringcol" hidden>
    <h3>Chord ring (2<sup>160</sup> identifier space)</h3>
    <div class="legend">
      <span><span class="dot"></span> node</span>
      <span><span class="dot entry"></span> entry node</span>
      <span><span class="diamond"></span> probed cluster</span>
    </div>
    <svg id="ring" viewBox="0 0 440 440" width="100%" role="img" aria-label="Chord ring with node and cluster positions"></svg>
  </div>
  <div id="routecol" hidden>
    <h3>Routing decision</h3>
    <div id="route"></div>
    <h3>Results</h3>
    <div id="results"></div>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
const shortId = s => '\\u2026' + BigInt(s).toString(16).slice(-6);
const shortName = a => a.split(':')[0];
// One lookup per cluster (clustered DHT) or a single synthetic one (semantic router)
const allLookups = rt => rt.lookups ||
  [{ cluster_id: rt.primary_cluster, owner: rt.primary_owner, hops: rt.chord_hops_to_owner, path: rt.lookup_path || [] }];

function pt(pct, r) {
  const a = (pct / 100) * 2 * Math.PI - Math.PI / 2;   // 0% at 12 o'clock, clockwise
  return [220 + r * Math.cos(a), 220 + r * Math.sin(a)];
}

function drawRing(d) {
  const R = 150, svg = [];
  svg.push(`<circle cx="220" cy="220" r="${R}" fill="none" stroke="var(--line)" stroke-width="2"/>`);
  svg.push(`<text x="220" y="52" text-anchor="middle" class="lbl2">0%</text>`);

  for (const c of d.routing.clusters) {
    const [x, y] = pt(c.ring_pct, R - 24);
    svg.push(`<g transform="translate(${x},${y})">
      <rect x="-5" y="-5" width="10" height="10" transform="rotate(45)" fill="var(--c-cluster)" stroke="var(--surface)" stroke-width="2">
        <title>Cluster ${c.cluster_id} \\u2014 ring ${c.ring_pct}% \\u2014 served by ${c.served_by || '?'}</title>
      </rect>
      <text y="-10" text-anchor="middle" class="lbl">C${c.cluster_id}</text>
    </g>`);
  }

  const pos = {};
  for (const n of d.ring) {
    if (n.status !== 'online') continue;
    pos[n.address] = n.ring_pct;
    const isEntry = n.address === d.entry_node;
    const [x, y] = pt(n.ring_pct, R);
    const [lx, ly] = pt(n.ring_pct, R + 26);
    svg.push(`<circle cx="${x}" cy="${y}" r="${isEntry ? 9 : 7}" fill="var(--c-node)" stroke="var(--surface)" stroke-width="2">
      <title>${n.address} \\u2014 id ${shortId(n.node_id)} \\u2014 ring ${n.ring_pct}%</title>
    </circle>`);
    if (isEntry) svg.push(`<circle cx="${x}" cy="${y}" r="14" fill="none" stroke="var(--c-node)" stroke-width="2"/>`);
    svg.push(`<text x="${lx}" y="${ly}" text-anchor="middle" dominant-baseline="middle" class="lbl" ${isEntry ? 'font-weight="700"' : ''}>${shortName(n.address)}</text>
      <text x="${lx}" y="${ly + 12}" text-anchor="middle" class="lbl2">${n.ring_pct}%</text>`);
  }

  // Dashed polylines tracing each lookup's actual path (entry -> hop nodes ->
  // owner). The semantic router has one lookup; the clustered DHT has one per
  // cluster (SHA-1 scatters them). Identical paths are merged into one line.
  const rt = d.routing;
  const lookups = allLookups(rt);
  const byChain = {};
  for (const lk of lookups) {
    const chain = [...new Set([...(lk.path || []), lk.owner])].filter(a => pos[a] != null);
    if (chain.length < 2) continue;
    const key = chain.join('|');
    (byChain[key] = byChain[key] || { chain, labels: [] }).labels
      .push(`C${lk.cluster_id}: ${lk.hops} hop${lk.hops === 1 ? '' : 's'}`);
  }
  for (const { chain, labels } of Object.values(byChain)) {
    const pts = chain.map(a => pt(pos[a], R));
    const poly = pts.map(p => p.join(',')).join(' ');
    const [mx, my] = [(pts[0][0] + pts[1][0]) / 2, (pts[0][1] + pts[1][1]) / 2];
    svg.unshift(`<polyline points="${poly}" fill="none" stroke="var(--ink-2)" stroke-width="1.5" stroke-dasharray="5 4"/>
      <text x="${mx}" y="${my - 6}" text-anchor="middle" class="lbl2">${labels.join(' \\u00b7 ')}</text>`);
  }
  $('ring').innerHTML = svg.join('');
}

function routeHtml(d) {
  const rt = d.routing;
  const perCluster = rt.clusters.some(c => c.lookup_hops != null);
  const rows = rt.clusters.map(c => `<tr>
    <td>C${c.cluster_id}${c.cluster_id === rt.primary_cluster ? ' \\u2605' : ''}</td>
    <td><code>${shortId(c.hash)}</code></td>
    <td>${c.ring_pct}%</td>
    <td>${c.served_by ? shortName(c.served_by) : '?'}</td>
    ${perCluster ? `<td>${c.lookup_hops}</td>` : ''}
  </tr>`).join('');
  const contacted = rt.nodes_contacted.map(n => `${shortName(n.node)} (C${n.clusters.join(', C')})`).join(' \\u2192 ');
  const lookups = allLookups(rt);

  // Per-lookup paths, plus per-node maps of which jumps were taken from it
  // (and for which clusters) and which clusters it answered via its successor.
  const pathsHtml = lookups.map(lk => {
    const p = (lk.path || []).map(shortName).join(' \\u2192 ');
    return `<div>C${lk.cluster_id}: ${p} \\u2192 <b>${shortName(lk.owner)}</b> <span class="muted">(${lk.hops} hop${lk.hops === 1 ? '' : 's'})</span></div>`;
  }).join('');
  const jumps = {}, answered = {};
  for (const lk of lookups) {
    const p = lk.path || [];
    for (let i = 0; i + 1 < p.length; i++) {
      ((jumps[p[i]] = jumps[p[i]] || {})[p[i + 1]] = jumps[p[i]][p[i + 1]] || []).push('C' + lk.cluster_id);
    }
    if (p.length) (answered[p[p.length - 1]] = answered[p[p.length - 1]] || []).push('C' + lk.cluster_id);
  }

  const fingerHtml = Object.keys(rt.finger_tables || {}).map(addr => {
    const ft = rt.finger_tables[addr];
    if (!ft) return '';
    // Aggregate the 160 fingers per distinct target node; the highest finger
    // preceding the lookup target is the jump closest_preceding_node picks.
    const agg = {};
    for (const g of ft) {
      const [a, b] = g.fingers.split('-').map(Number);
      const n = agg[g.node] ?? (agg[g.node] = { count: 0, top: -1, topStart: null });
      n.count += b - a + 1;
      if (b > n.top) { n.top = b; n.topStart = g.start_pct; }
    }
    const ftRows = Object.entries(agg).sort((x, y) => y[1].top - x[1].top).map(([node, a]) => {
      const takenFor = (jumps[addr] || {})[node];
      const self = node === addr;
      return `<tr${takenFor ? ' style="font-weight:700"' : ''}>
        <td>${shortName(node)}${self ? ' (itself)' : ''}${takenFor ? ` \\u2190 jump taken (${takenFor.join(', ')})` : ''}</td>
        <td>${a.count}</td>
        <td>i = ${a.top}</td>
        <td>${a.topStart}%</td>
      </tr>`;
    }).join('');
    return `<details open>
      <summary><b>${shortName(addr)}</b> finger table${answered[addr] ? ` (answered ${answered[addr].join(', ')} via its successor)` : ''}</summary>
      <table>
        <tr><th>Points to</th><th># fingers</th><th>Highest finger</th><th>It covers from</th></tr>
        ${ftRows}
      </table>
      <p class="muted">Fingers still pointing at the node itself are not yet converged (fix_fingers repairs one random finger per stabilization tick).</p>
    </details>`;
  }).join('');

  return `
    <p>Entry node <b>${rt.entry_node}</b> <code>${shortId(rt.entry_node_id)}</code> (ring ${rt.entry_node_ring_pct}%)
    vectorized the query and picked cluster <b>C${rt.primary_cluster}</b>${rt.clusters.length > 1 ? ` (+${rt.clusters.length - 1} adjacent)` : ''}.
    Chord lookup reached owner <b>${shortName(rt.primary_owner)}</b> in <b>${rt.chord_hops_to_owner}</b> hop(s).</p>
    <p class="muted" style="margin-bottom:.2rem">Lookup path${lookups.length > 1 ? 's (one Chord lookup per cluster)' : ''}:</p>
    <div class="paths">${pathsHtml}</div>
    <table>
      <tr><th>Cluster (\\u2605 = primary)</th><th>Hash</th><th>Ring position</th><th>Served by</th>${perCluster ? '<th>Lookup hops</th>' : ''}</tr>
      ${rows}
    </table>
    <h3>Finger tables along the lookup path</h3>
    ${fingerHtml}
    <p class="muted">Nodes contacted: ${contacted}</p>`;
}

$('f').addEventListener('submit', async (e) => {
  e.preventDefault();
  $('status').textContent = 'Routing query through the ring\\u2026';
  $('meta').hidden = $('ringcol').hidden = $('routecol').hidden = true;
  try {
    const r = await fetch('/query', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ query: $('q').value, nprobe: +$('nprobe').value })
    });
    const d = await r.json();
    if (!r.ok) throw new Error(JSON.stringify(d.detail));
    $('status').textContent = '';
    $('entry').textContent = d.entry_node;
    $('cl').textContent = d.routing.clusters.map(c => 'C' + c.cluster_id).join(', ');
    $('hops').textContent = d.hops;
    $('lat').textContent = d.latency_ms.toFixed(0) + ' ms';
    $('meta').hidden = false;
    drawRing(d);
    $('route').innerHTML = routeHtml(d);
    $('results').innerHTML = d.results.map(c => `
      <div class="card">
        <span class="sim">${(c.similarity ?? 0).toFixed(3)}</span>
        <b>${c.course_title}</b><br>
        <small>${c.category || ''}</small>
        <div>${(c.description || '').slice(0, 180)}</div>
      </div>`).join('') || '<p>No results.</p>';
    $('ringcol').hidden = $('routecol').hidden = false;
  } catch (err) {
    $('status').textContent = 'Error: ' + err.message;
  }
});
</script>
</body>
</html>"""


GATEWAY_TITLE = os.environ.get("GATEWAY_TITLE", "P2P Semantic Router")


@app.get("/", response_class=HTMLResponse)
def demo_page() -> str:
    return DEMO_PAGE.replace("__TITLE__", GATEWAY_TITLE)
