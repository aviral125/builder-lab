"""HTTP API and the single plain page.

Run with `python -m app.main`. The server binds to 127.0.0.1 unless HOST says
otherwise: it can sign orders with the keys in .env and has no authentication,
so it must not be exposed to a network.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse

from app.config import Settings, load_settings
from app.db import Database
from app.service import TradingService, build_venues
from app.venues.base import NotConfigured, VenueError

STATIC = Path(__file__).parent / "static"
log = logging.getLogger("builder_lab")


def create_app(
    settings: Settings | None = None,
    service: TradingService | None = None,
    *,
    start_poller: bool = True,
) -> FastAPI:
    settings = settings or load_settings()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        clients = None
        svc = service
        if svc is None:
            venues, clients = build_venues(settings)
            svc = TradingService(settings, Database(settings.db_path), venues)
        app.state.service = svc
        task = asyncio.create_task(svc.poll_forever()) if start_poller else None
        try:
            yield
        finally:
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            for venue in svc.venues.values():
                await venue.close()
            if clients is not None:
                await clients.close()

    app = FastAPI(title="builder-lab", lifespan=lifespan)

    def svc(request: Request) -> TradingService:
        return request.app.state.service

    @app.exception_handler(NotConfigured)
    async def _not_configured(_: Request, exc: NotConfigured):
        return JSONResponse(status_code=409, content={"error": str(exc), "kind": "not_configured"})

    @app.exception_handler(VenueError)
    async def _venue_error(_: Request, exc: VenueError):
        return JSONResponse(status_code=400, content={"error": str(exc), "kind": "venue_error"})

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception):
        log.exception("unhandled error")
        return JSONResponse(
            status_code=502, content={"error": f"{type(exc).__name__}: {exc}", "kind": "upstream"}
        )

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/config")
    async def config(request: Request):
        s = svc(request)
        view = settings.public_view()
        view["venues"] = sorted(s.venues)
        view["builders"] = {name: v.builder_identity() for name, v in s.venues.items()}
        return view

    # --- market data --------------------------------------------------------
    @app.get("/api/markets")
    async def markets(request: Request, venue: str, q: str | None = None, limit: int = Query(20, ge=1, le=100)):
        return await svc(request).venue(venue).markets(q, limit)

    @app.get("/api/book")
    async def book(request: Request, venue: str, symbol: str):
        return await svc(request).venue(venue).orderbook(symbol)

    # --- account ------------------------------------------------------------
    @app.get("/api/account")
    async def account(request: Request, venue: str):
        return await svc(request).venue(venue).account()

    @app.get("/api/orders")
    async def orders(request: Request, venue: str | None = None, limit: int = Query(100, ge=1, le=500)):
        return svc(request).db.list_orders(venue, limit)

    @app.post("/api/orders")
    async def place_order(request: Request, payload: dict[str, Any] = Body(...)):
        return await svc(request).place_order(payload)

    @app.delete("/api/orders")
    async def cancel_order(request: Request, venue: str, order_id: str, symbol: str = ""):
        return await svc(request).cancel(venue, symbol, order_id)

    @app.get("/api/fills")
    async def fills(request: Request, venue: str | None = None, limit: int = Query(100, ge=1, le=500)):
        return svc(request).db.list_fills(venue, limit)

    @app.post("/api/fills/sync")
    async def sync_fills(request: Request):
        return await svc(request).sync_fills()

    # --- builder ------------------------------------------------------------
    @app.get("/api/builder/status")
    async def builder_status(request: Request, venue: str):
        return await svc(request).venue(venue).builder_status()

    @app.post("/api/builder/approve")
    async def builder_approve(request: Request, payload: dict[str, Any] = Body(...)):
        return await svc(request).approve_builder(
            str(payload.get("venue", "")), payload.get("max_fee_bps")
        )

    @app.get("/api/builder/approvals")
    async def builder_approvals(request: Request, venue: str | None = None):
        return svc(request).db.list_approvals(venue)

    @app.get("/api/builder/earnings")
    async def builder_earnings(request: Request, venue: str):
        return await svc(request).builder_earnings(venue)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = load_settings()
    if settings.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("HOST=%s exposes an unauthenticated server that can sign orders", settings.host)
    log.info("starting builder-lab (dry_run=%s, hyperliquid=%s)", settings.dry_run, settings.hl_network)
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
