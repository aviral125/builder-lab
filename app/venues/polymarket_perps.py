"""Polymarket Perps.

The builder is identified by its Perps account address and must be registered
and enabled by Polymarket. The trader approves a max fee once; the trader's
session is then opened with that builder selected, and every order placed
through the session is attributed.

The SDK marks the perps API as experimental, so this adapter keeps its surface
small and converts everything to plain JSON at the edge.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from app.signer import Signer
from app.venues.base import NotConfigured, OrderRequest, OrderResult, Venue, VenueError, to_jsonable
from app.venues.polymarket_common import PolymarketClients

BPS = Decimal(10_000)


def bps_to_rate(bps: Decimal) -> str:
    """Perps fee rates are fractions of notional: 3 bps -> '0.0003'."""
    return format((bps / BPS).normalize(), "f")


def normalize_fill(f: Any) -> dict:
    d = to_jsonable(f)
    ts = getattr(f, "timestamp", None)
    return {
        "fill_id": str(d.get("trade_id")),
        "order_id": str(d.get("order_id")),
        "symbol": str(d.get("instrument_id")),
        "side": "BUY" if d.get("side") == "long" else "SELL",
        "price": str(d.get("price")),
        "size": str(d.get("quantity")),
        "fee": str(d.get("fee")),
        "builder_fee": str(d.get("builder_fee")),
        "ts": int(ts.timestamp() * 1000) if ts else None,
        "raw": d,
    }


def builder_is_active(status: Any) -> bool:
    return bool(status.registered and status.enabled and status.admission_enabled)


class PolymarketPerpsVenue(Venue):
    name = "polymarket_perps"

    def __init__(
        self,
        clients: PolymarketClients,
        *,
        builder_address: str | None,
        builder_fee_bps: Decimal,
        builder_signer: Signer | None = None,
    ) -> None:
        self._clients = clients
        self._builder = builder_address
        self._fee_bps = builder_fee_bps
        self._builder_signer = builder_signer
        self._session = None
        self._session_has_builder = False
        self._lock = asyncio.Lock()

    def trader_address(self) -> str | None:
        return self._clients.wallet_if_known()

    async def _builder_status(self) -> Any | None:
        if not self._builder:
            return None
        return await self._clients.public().fetch_perps_builder_status(address=self._builder)

    async def _get_session(self):
        """Trader session, opened with the builder selected when the builder is active."""
        client = await self._clients.secure()
        async with self._lock:
            if self._session is not None and not self._session.closed:
                return self._session
            attribution = None
            status = await self._builder_status()
            if status is not None and builder_is_active(status):
                attribution = self._builder
            self._session = await client.open_perps_session(
                builder_attribution=attribution, label="builder-lab"
            )
            self._session_has_builder = attribution is not None
            return self._session

    # --- market data --------------------------------------------------------
    async def markets(self, query: str | None = None, limit: int = 20) -> Any:
        public = self._clients.public()
        instruments, tickers = await asyncio.gather(
            public.fetch_perps_instruments(), public.fetch_perps_tickers()
        )
        marks = {t.instrument_id: t for t in tickers}
        out = []
        for inst in instruments:
            if query and query.lower() not in inst.symbol.lower():
                continue
            ticker = marks.get(inst.id)
            out.append(
                {
                    "symbol": str(inst.id),
                    "name": inst.symbol,
                    "category": to_jsonable(inst.category),
                    "mark_price": str(ticker.mark_price) if ticker else None,
                    "max_leverage": inst.max_leverage,
                    "min_notional": str(inst.min_notional),
                    "quantity_decimals": inst.quantity_decimals,
                    "price_decimals": inst.price_decimals,
                }
            )
            if len(out) >= limit:
                break
        return out

    async def orderbook(self, symbol: str) -> Any:
        book = await self._clients.public().fetch_perps_book(instrument_id=int(symbol), depth=20)
        return to_jsonable(book)

    async def reference_price(self, symbol: str, side: str) -> Decimal:
        ticker = await self._clients.public().fetch_perps_ticker(instrument_id=int(symbol))
        return Decimal(str(ticker.mark_price))

    # --- account ------------------------------------------------------------
    async def account(self) -> Any:
        session = await self._get_session()
        balances, portfolio, orders = await asyncio.gather(
            session.fetch_balances(), session.fetch_portfolio(), session.fetch_open_orders()
        )
        return {
            "address": self.trader_address(),
            "balances": to_jsonable(balances),
            "portfolio": to_jsonable(portfolio),
            "open_orders": to_jsonable(orders),
            "session_builder": to_jsonable(session.builder_attribution),
        }

    async def place_order(self, req: OrderRequest) -> OrderResult:
        session = await self._get_session()
        kwargs: dict[str, Any] = {
            "instrument_id": int(req.symbol),
            "side": req.side,
            "quantity": req.size,
            "reduce_only": req.reduce_only,
        }
        if req.price is None:
            tif = req.tif if req.tif in ("ioc", "fok") else "ioc"
            kwargs["time_in_force"] = tif
        else:
            kwargs["time_in_force"] = req.tif
            kwargs["price"] = req.price
            if req.tif == "gtc":
                kwargs["post_only"] = req.post_only
        placement = await session.place_order(**kwargs)
        order = placement.order
        raw = to_jsonable(placement)
        if order.status == "filled":
            status = "filled"
        elif order.status in ("accepted", "open", "partial"):
            status = "open"
        else:  # cancelled, post_only_rejected, fok_unfilled, ioc_no_fill, ...
            status = "rejected"
        attributed = getattr(order, "builder", None)
        return OrderResult(
            status=status,
            error=None if status != "rejected" else f"order ended as {order.status}",
            venue_order_id=str(order.id),
            builder=attributed.address if attributed else None,
            builder_fee_bps=str(attributed.fee_rate * BPS) if attributed else None,
            raw=raw,
        )

    async def cancel(self, symbol: str, order_id: str) -> Any:
        session = await self._get_session()
        result = await session.cancel_order(order_id=int(order_id))
        if getattr(result, "status", "ok") != "ok":
            raise VenueError(f"cancel rejected: {to_jsonable(result)}")
        return to_jsonable(result)

    async def fills(self) -> list[dict]:
        session = await self._get_session()
        page = await session.list_fills().first_page()
        return [normalize_fill(f) for f in page.items]

    # --- builder ------------------------------------------------------------
    def builder_identity(self) -> dict:
        return {"builder": self._builder, "max_fee_bps": str(self._fee_bps)}

    def _require_builder(self) -> str:
        if not self._builder:
            raise NotConfigured("POLY_PERPS_BUILDER_ADDRESS is not set")
        return self._builder

    async def builder_status(self) -> Any:
        builder = self._require_builder()
        status = await self._builder_status()
        out: dict[str, Any] = {
            "builder": builder,
            "registered": status.registered,
            "enabled": status.enabled,
            "admission_enabled": status.admission_enabled,
            "active": builder_is_active(status),
            "venue_max_fee_bps": str(status.max_fee_rate * BPS),
        }
        if not out["active"]:
            out["note"] = (
                "Builder is not active on Polymarket Perps yet, so orders go out without "
                "attribution. Ask Polymarket (builder@polymarket.com) to enable this address."
            )
        if self._clients.has_trader:
            session = await self._get_session()
            approvals = await session.fetch_builder_approvals(builder=builder)
            latest = max(approvals, key=lambda a: a.approval_version, default=None)
            out["trader"] = self.trader_address()
            out["trader_approved_max_fee_bps"] = str(latest.max_fee_rate * BPS) if latest else "0"
            out["session_attribution"] = to_jsonable(session.builder_attribution)
        return out

    async def approve_builder(self, max_fee_bps: Decimal) -> Any:
        builder = self._require_builder()
        session = await self._get_session()
        if max_fee_bps == 0:
            approval = await session.revoke_builder_fee(builder)
        else:
            approval = await session.approve_builder_fee(
                builder_address=builder, max_fee_rate=bps_to_rate(max_fee_bps)
            )
        if not self._session_has_builder:
            # Reopen next time so the session picks the builder up once it is active.
            await session.close()
            self._session = None
        return to_jsonable(approval)

    async def builder_earnings(self) -> Any:
        self._require_builder()
        if self._builder_signer is None:
            raise NotConfigured(
                "POLY_PERPS_BUILDER_PRIVATE_KEY is not set. Perps builder earnings can only be "
                "read by the builder account. Fills in the local database still show the "
                "builder fee paid on each attributed trade."
            )
        from polymarket import AsyncSecureClient

        client = await AsyncSecureClient.create(private_key=self._builder_signer.private_key())
        try:
            session = await client.open_perps_session(label="builder-lab-earnings")
            try:
                summary = await session.fetch_builder_earnings_summary()
                page = await session.list_builder_earnings().first_page()
                return {"summary": to_jsonable(summary), "recent": to_jsonable(page.items)}
            finally:
                await session.close()
        finally:
            await client.close()

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None
