"""Receipt time and server processing interval for API requests (developer timing instrumentation).

Definitions (see README "Timing instrumentation"):
  - Application receipt time (request entry): this engine's UTC wall clock, read once by this middleware when the
    application begins handling the request, after the server has parsed its headers and before the body is read,
    parsed and validated. It is NOT when the first byte reached the computer or the server's socket; time spent in
    the network stack and in uvicorn before the application sees the request is not included. For an accepted sample it is stored with the row
    as receipt metadata (`{"received_at": ..., "origin": "this_engine"}`); it is never the sample's identity or order.
  - Server processing interval: the monotonic time from that same point to the start of the response, sent as
    `Server-Timing: app;dur=<milliseconds>`. For a sample POST it covers body read, validation, the wait for the
    database lock, the write and the commit. It is not stored, and it excludes network time on either side. Monotonic
    readings are only ever subtracted within this process; none is persisted or compared across restarts or machines.
Rejected requests (invalid bodies, future-dated samples) store nothing, so no receipt exists for them.
The middleware is outermost, so it also adds `Server-Timing` to `Access-Control-Expose-Headers` for cross-origin
requests (merged with whatever a route already exposes), letting the dashboard read the header.
"""

import time

from app.core import clock

STATE_KEY = "athletrix_received_at"


class TimingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter_ns()
        scope.setdefault("state", {})[STATE_KEY] = clock.receipt_now()
        has_origin = any(k == b"origin" for k, _ in scope.get("headers", []))

        async def timed_send(message):
            if message["type"] == "http.response.start":
                dur_ms = (time.perf_counter_ns() - start) / 1e6
                headers = [(k, v) for k, v in message.get("headers", []) if k.lower() != b"server-timing"]
                headers.append((b"server-timing", f'app;dur={dur_ms:.3f};desc="engine receipt to response start"'.encode()))
                if has_origin:
                    exposed = [v.decode() for k, v in headers if k.lower() == b"access-control-expose-headers"]
                    names = [n.strip() for v in exposed for n in v.split(",") if n.strip()]
                    if "server-timing" not in {n.lower() for n in names}:
                        names.append("Server-Timing")
                    headers = [(k, v) for k, v in headers if k.lower() != b"access-control-expose-headers"]
                    headers.append((b"access-control-expose-headers", ", ".join(names).encode()))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, timed_send)


def received_at(request):
    """The receipt time recorded for this request by the middleware (the clock now if the middleware is not installed)."""
    return getattr(request.state, STATE_KEY, None) or clock.receipt_now()
