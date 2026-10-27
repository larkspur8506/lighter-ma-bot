"""HTTP entrypoint for the container.

T1 ships only a liveness endpoint: the image's default CMD is
``uvicorn app.main:app``, and a container whose CMD cannot import its app is
not "up" in any useful sense. Keeping exactly one route here means the
Dockerfile, the HEALTHCHECK and the CI smoke-run all exercise the same code
path that production starts with.

T10 replaces this module with the real monitoring panel (auth, read-only
views, the three write operations). Nothing else should be added here before
then.
"""

from __future__ import annotations

import time

from fastapi import FastAPI

SERVICE = "lighter-ma-bot"
VERSION = "0.1.0"

_STARTED_AT = time.time()

# No docs/openapi routes: the panel is the only intended surface, and T10 will
# decide which routes need the admin token. Fewer default routes = less to
# accidentally expose.
app = FastAPI(
    title=SERVICE,
    version=VERSION,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.get("/health")
def health() -> dict:
    """Liveness probe. Must stay dependency-free: no DB, no network, no SDK."""
    return {
        "ok": True,
        "service": SERVICE,
        "version": VERSION,
        "uptime_s": round(time.time() - _STARTED_AT, 3),
    }
