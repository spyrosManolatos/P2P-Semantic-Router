import httpx


class JSONRPCProxy:
    """Drop-in replacement for xmlrpc.client.ServerProxy's call interface
    (proxy.method(*args) -> result), talking instead to the async_semantic_router
    node's generic POST /rpc/{method} endpoint. Lets the containerized benchmark
    harness (evaluate.py) address the FastAPI/httpx architecture with the same
    call sites used for the xmlrpc-based architectures."""

    def __init__(self, address: str, timeout: float = 30.0):
        self._address = address
        self._timeout = timeout

    def __getattr__(self, name: str):
        def _call(*args, **kwargs):
            resp = httpx.post(
                f"http://{self._address}/rpc/{name}",
                json={"args": args, "kwargs": kwargs},
                timeout=self._timeout,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"RPC '{name}' to {self._address} failed: {resp.status_code} {resp.text}")
            payload = resp.json()
            if "error" in payload:
                raise RuntimeError(f"RPC '{name}' to {self._address} raised: {payload['error']}")
            return payload["result"]
        return _call

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
