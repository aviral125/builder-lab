"""Order flow shared by all venues: validation, safety rails, persistence."""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal, InvalidOperation
from typing import Any

from app.config import Settings
from app.db import Database
from app.signer import EnvKeySigner
from app.venues.base import OrderRequest, OrderResult, Venue, VenueError
from app.venues.hyperliquid import HyperliquidVenue
from app.venues.polymarket_clob import PolymarketClobVenue
from app.venues.polymarket_common import PolymarketClients
from app.venues.polymarket_perps import PolymarketPerpsVenue

log = logging.getLogger("builder_lab")

SIDES = ("BUY", "SELL")
TIFS = ("gtc", "ioc", "fok")


def build_venues(settings: Settings) -> tuple[dict[str, Venue], PolymarketClients]:
    poly_trader = EnvKeySigner(settings.poly_private_key) if settings.poly_private_key else None
    hl_trader = EnvKeySigner(settings.hl_private_key) if settings.hl_private_key else None
    perps_builder = (
        EnvKeySigner(settings.perps_builder_private_key)
        if settings.perps_builder_private_key
        else None
    )
    if (
        perps_builder
        and poly_trader
        and perps_builder.address.lower() == poly_trader.address.lower()
    ):
        raise VenueError("Perps builder and trader must be different accounts")

    clients = PolymarketClients(poly_trader, settings.poly_wallet_address)
    venues: dict[str, Venue] = {
        "polymarket": PolymarketClobVenue(clients, settings.poly_builder_code),
        "polymarket_perps": PolymarketPerpsVenue(
            clients,
            builder_address=settings.perps_builder_address,
            builder_fee_bps=settings.perps_builder_fee_bps,
            builder_signer=perps_builder,
        ),
        "hyperliquid": HyperliquidVenue(
            network=settings.hl_network,
            trader=hl_trader,
            account_address=settings.hl_account_address,
            builder_address=settings.hl_builder_address,
            builder_fee_bps=settings.hl_builder_fee_bps,
        ),
    }
    return venues, clients


def parse_decimal(name: str, value: Any, *, allow_none: bool = False) -> Decimal | None:
    if value is None or value == "":
        if allow_none:
            return None
        raise VenueError(f"{name} is required")
    try:
        d = Decimal(str(value))
    except InvalidOperation as exc:
        raise VenueError(f"{name} must be a number") from exc
    if not d.is_finite() or d <= 0:
        raise VenueError(f"{name} must be positive")
    return d


class TradingService:
    def __init__(self, settings: Settings, db: Database, venues: dict[str, Venue]) -> None:
        self.settings = settings
        self.db = db
        self.venues = venues

    def venue(self, name: str) -> Venue:
        try:
            return self.venues[name]
        except KeyError as exc:
            raise VenueError(f"unknown venue {name!r}; use one of {sorted(self.venues)}") from exc

    def make_request(self, payload: dict[str, Any]) -> OrderRequest:
        venue = str(payload.get("venue", ""))
        self.venue(venue)
        symbol = str(payload.get("symbol") or "").strip()
        if not symbol:
            raise VenueError("symbol is required")
        side = str(payload.get("side") or "").upper()
        if side not in SIDES:
            raise VenueError("side must be BUY or SELL")
        price = parse_decimal("price", payload.get("price"), allow_none=True)
        tif = str(payload.get("tif") or ("gtc" if price is not None else "ioc")).lower()
        if tif not in TIFS:
            raise VenueError("tif must be gtc, ioc or fok")
        if price is None and tif == "gtc":
            raise VenueError("a gtc order needs a price")
        post_only = bool(payload.get("post_only", False))
        if post_only and (price is None or tif != "gtc"):
            raise VenueError("post_only needs a gtc limit order")
        return OrderRequest(
            venue=venue,
            symbol=symbol,
            side=side,
            size=parse_decimal("size", payload.get("size")),
            price=price,
            tif=tif,
            post_only=post_only,
            reduce_only=bool(payload.get("reduce_only", False)),
        )

    async def place_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        req = self.make_request(payload)
        venue = self.venue(req.venue)

        # Safety rail 1: cap the notional. Market orders are priced off the venue.
        ref_price = req.price
        if ref_price is None:
            ref_price = await venue.reference_price(req.symbol, req.side)
        notional = ref_price * req.size
        if notional > self.settings.max_order_usd:
            raise VenueError(
                f"order notional ${notional:.2f} exceeds MAX_ORDER_USD "
                f"(${self.settings.max_order_usd})"
            )

        # Safety rail 2: dry run never reaches the venue.
        if self.settings.dry_run:
            identity = venue.builder_identity()
            result = OrderResult(
                status="dry_run",
                builder=identity.get("builder") or identity.get("builder_code"),
                builder_fee_bps=identity.get("fee_bps") or identity.get("max_fee_bps"),
                raw={"would_send": payload, "builder": identity},
            )
        else:
            try:
                result = await venue.place_order(req)
            except VenueError:
                raise
            except Exception as exc:  # SDK and transport errors
                result = OrderResult(status="rejected", error=f"{type(exc).__name__}: {exc}")

        row = {
            "venue": req.venue,
            "trader": venue.trader_address(),
            "symbol": req.symbol,
            "side": req.side,
            "size": str(req.size),
            "price": str(req.price) if req.price is not None else None,
            "tif": req.tif,
            "status": result.status,
            "venue_order_id": result.venue_order_id,
            "builder": result.builder,
            "builder_fee_bps": result.builder_fee_bps,
            "dry_run": self.settings.dry_run,
            "error": result.error,
            "raw": result.raw,
        }
        row["id"] = self.db.insert_order(row)
        row["notional_usd"] = str(notional)
        return row

    async def cancel(self, venue_name: str, symbol: str, order_id: str) -> Any:
        if self.settings.dry_run:
            return {"dry_run": True, "would_cancel": order_id}
        result = await self.venue(venue_name).cancel(symbol, order_id)
        self.db.mark_cancelled(venue_name, order_id)
        return result

    async def approve_builder(self, venue_name: str, max_fee_bps: Any) -> Any:
        venue = self.venue(venue_name)
        try:
            fee = Decimal(str(max_fee_bps))
        except InvalidOperation as exc:
            raise VenueError("max_fee_bps must be a number") from exc
        if fee < 0 or fee > 10:
            raise VenueError("max_fee_bps must be between 0 and 10")
        identity = venue.builder_identity()
        if self.settings.dry_run:
            return {"dry_run": True, "would_approve_bps": str(fee), "builder": identity}
        result = await venue.approve_builder(fee)
        self.db.insert_approval(
            venue_name,
            venue.trader_address() or "",
            str(identity.get("builder") or identity.get("builder_code") or ""),
            str(fee),
            result,
        )
        return result

    async def builder_earnings(self, venue_name: str) -> Any:
        venue = self.venue(venue_name)
        result = await venue.builder_earnings()
        identity = venue.builder_identity()
        self.db.insert_earnings_snapshot(
            venue_name, str(identity.get("builder") or identity.get("builder_code") or ""), result
        )
        return result

    async def sync_fills(self) -> dict[str, int | str]:
        """Pull recent fills for every venue that has a trader configured."""
        out: dict[str, int | str] = {}
        for name, venue in self.venues.items():
            try:
                fills = await venue.fills()
                trader = venue.trader_address() or ""
                out[name] = self.db.upsert_fills(name, trader, fills)
            except VenueError as exc:  # includes NotConfigured
                out[name] = f"skipped: {exc}"
            except Exception as exc:
                log.warning("fill sync failed for %s: %s", name, exc)
                out[name] = f"error: {type(exc).__name__}"
        return out

    async def poll_forever(self) -> None:
        while True:
            try:
                await self.sync_fills()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("fill poller iteration failed")
            await asyncio.sleep(self.settings.poll_seconds)
