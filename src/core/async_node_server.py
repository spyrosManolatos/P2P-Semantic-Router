import asyncio
from contextlib import asynccontextmanager
from typing import Optional

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def build_app(node, bootstrap_addr: Optional[str]) -> FastAPI:
    """Generic FastAPI app for any async Chord node: dispatches POST /rpc/{method}
    to the matching method on `node` (awaiting it if it's a coroutine function),
    and drives node.join()/start()/stop() through the ASGI lifespan. Shared across
    architectures.async_semantic_router, .async_clustered_dht and .async_standard_dht,
    which all expose the same join/start/stop/shutdown_event interface."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # join() returns False if the bootstrap peer wasn't reachable yet (it is
        # commonly still parsing its ~30MB centroid artifact when the next
        # container comes up). Without a retry the node stays permanently
        # orphaned -- successor == self, predecessor == None -- and nothing in
        # the stabilize protocol ever recovers it, so the ring never converges
        # to a single cycle. Retry with backoff instead of failing silently.
        if bootstrap_addr:
            delay = 1.0
            for attempt in range(1, 11):
                if await node.join(bootstrap_addr):
                    break
                print(f"[{node.address}] join attempt {attempt} via {bootstrap_addr} failed; "
                      f"retrying in {delay:.1f}s")
                await asyncio.sleep(delay)
                delay = min(delay * 1.6, 10.0)
            else:
                print(f"[{node.address}] WARNING: could not join ring via {bootstrap_addr} "
                      f"after 10 attempts; starting orphaned.")
        else:
            await node.join(bootstrap_addr)
        await node.start()
        yield
        if not node.shutdown_event.is_set():
            await node.stop()

    app = FastAPI(lifespan=lifespan)

    @app.post("/rpc/{method_name}")
    async def rpc_dispatch(method_name: str, request: Request):
        body = await request.json()
        args = body.get("args", [])
        kwargs = body.get("kwargs", {})

        fn = getattr(node, method_name, None)
        if fn is None or method_name.startswith("_"):
            return JSONResponse({"error": f"Unknown method '{method_name}'"}, status_code=404)

        try:
            if asyncio.iscoroutinefunction(fn):
                result = await fn(*args, **kwargs)
            else:
                result = fn(*args, **kwargs)
            return JSONResponse({"result": result})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    return app


def run_node(node, ip: str, port: int, bootstrap: Optional[str], label: str):
    app = build_app(node, bootstrap)

    bind_ip = ip
    if ip not in ["127.0.0.1", "localhost"]:
        bind_ip = "0.0.0.0"

    config = uvicorn.Config(app, host=bind_ip, port=port, log_level="warning")
    server = uvicorn.Server(config)
    node._uvicorn_server = server

    print("=" * 60)
    print(f"Starting {label} DHT Node on announce address {ip}:{port}")
    if bootstrap:
        print(f"Bootstrapping via node: {bootstrap}")
    else:
        print("Starting as Bootstrap (Ring Initializer) Node.")
    print("=" * 60)

    server.run()
