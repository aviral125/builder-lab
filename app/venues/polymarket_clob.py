"""Polymarket prediction markets (CLOB).

The builder code is a public bytes32 from polymarket.com/settings?tab=builder.
It is part of the signed order, so attribution only needs `builder_code` on
every order. Fee rates are set on the builder profile, not per order, and the
trader does not sign a separate approval.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.venues.base import NotConfigured, OrderRequest, OrderResult, Venue, VenueError, to_jsonable
from app.venues.polymarket_common import PolymarketClients

COLLATERAL_DECIMALS = Decimal(10) ** 6


def market_view(market: Any) -> dict:
    """One market as the UI shows it: a question and its tradable outcomes."""
    legs = []
    outcomes = getattr(market, "outcomes", None)
    for key in ("yes", "no"):
        outcome = getattr(outcomes, key, None)
        token = getattr(outcome, "token_id", None)
        if outcome is None or token is None:
            continue
        price = getattr(outcome, "price", None)
        legs.append(
            {
                "label": getattr(outcome, "label", key.title()),
                "symbol": str(token),
                "price": str(price) if price is not None else None,
            }
        )
    return {
        "title": getattr(market, "question", None) or getattr(market, "slug", ""),
        "subtitle": getattr(market, "slug", None),
        "legs": legs,
        "id": str(getattr(market, "id", "")),
        "condition_id": str(getattr(market, "condition_id", "")),
    }


def normalize_trade(t: Any) -> dict:
    d = to_jsonable(t)
    matched = getattr(t, "matched_at", None)
    return {
        "fill_id": str(d.get("id")),
        "order_id": d.get("taker_order_id"),
        "symbol": str(d.get("asset_id")),
        "side": d.get("side"),
        "price": str(d.get("price")),
        "size": str(d.get("size")),
        "fee": None,  # the trade reports a fee rate, not an amount
        "builder_fee": None,
        "ts": int(matched.timestamp() * 1000) if matched else None,
        "raw": d,
    }


class PolymarketClobVenue(Venue):
    name = "polymarket"

    def __init__(self, clients: PolymarketClients, builder_code: str | None) -> None:
        self._clients = clients
        self._code = builder_code

    def trader_address(self) -> str | None:
        return self._clients.wallet_if_known()

    # --- market data --------------------------------------------------------
    async def markets(self, query: str | None = None, limit: int = 20) -> Any:
        public = self._clients.public()
        if query:
            page = await public.search(q=query, page_size=limit).first_page()
            out = []
            for result in page.items:
                for event in result.events:
                    for market in getattr(event, "markets", None) or ():
                        out.append(market_view(market))
            return out[:limit]
        try:  # busiest markets first
            page = await public.list_markets(
                closed=False, order="volume24hr", ascending=False, page_size=limit
            ).first_page()
        except Exception:
            page = await public.list_markets(closed=False, page_size=limit).first_page()
        return [market_view(m) for m in page.items]

    async def orderbook(self, symbol: str) -> Any:
        book = await self._clients.public().get_order_book(token_id=symbol)
        # Best prices first on both sides.
        bids = sorted(book.bids, key=lambda l: l.price, reverse=True)
        asks = sorted(book.asks, key=lambda l: l.price)
        return {
            "symbol": symbol,
            "bids": [{"price": str(l.price), "size": str(l.size)} for l in bids],
            "asks": [{"price": str(l.price), "size": str(l.size)} for l in asks],
            "tick_size": str(book.tick_size),
            "min_order_size": str(book.min_order_size),
        }

    async def reference_price(self, symbol: str, side: str) -> Decimal:
        # Price a taker pays on this side.
        return Decimal(str(await self._clients.public().get_price(token_id=symbol, side=side)))

    # --- account ------------------------------------------------------------
    async def account(self) -> Any:
        client = await self._clients.secure()
        wallet = str(client.wallet)
        balance = await client.get_balance_allowance(asset_type="COLLATERAL")
        positions = await client.list_positions(user=wallet).first_page()
        orders = await client.list_open_orders().first_page()
        return {
            "address": wallet,
            "signer": str(client.signer),
            "collateral": str(Decimal(balance.balance) / COLLATERAL_DECIMALS),
            "positions": to_jsonable(positions.items),
            "open_orders": to_jsonable(orders.items),
        }

    async def place_order(self, req: OrderRequest) -> OrderResult:
        client = await self._clients.secure()
        if req.reduce_only:
            raise VenueError("reduce_only does not apply to prediction markets")
        if req.price is not None and req.tif == "gtc":
            resp = await client.place_limit_order(
                token_id=req.symbol,
                price=req.price,
                size=req.size,
                side=req.side,
                post_only=req.post_only,
                builder_code=self._code,
            )
        else:
            limits: dict[str, Any] = {}
            if req.price is not None:
                limits["max_price" if req.side == "BUY" else "min_price"] = req.price
            resp = await client.place_market_order(
                token_id=req.symbol,
                side=req.side,
                shares=req.size,
                order_type="FOK" if req.tif == "fok" else "FAK",
                builder_code=self._code,
                **limits,
            )
        raw = to_jsonable(resp)
        if not getattr(resp, "ok", False):
            return OrderResult(
                status="rejected",
                error=f"{getattr(resp, 'code', '')}: {getattr(resp, 'message', '')}",
                raw=raw,
                builder=self._code,
            )
        status = "filled" if resp.status == "matched" else "open"
        return OrderResult(status=status, venue_order_id=str(resp.order_id), raw=raw, builder=self._code)

    async def cancel(self, symbol: str, order_id: str) -> Any:
        client = await self._clients.secure()
        return to_jsonable(await client.cancel_order(order_id=order_id))

    async def fills(self) -> list[dict]:
        client = await self._clients.secure()
        page = await client.list_account_trades().first_page()
        return [normalize_trade(t) for t in page.items]

    # --- builder ------------------------------------------------------------
    def builder_identity(self) -> dict:
        return {
            "builder_code": self._code,
            "note": "Fee rates are set on the builder profile at polymarket.com/settings?tab=builder.",
        }

    def _require_code(self) -> str:
        if not self._code:
            raise NotConfigured("POLY_BUILDER_CODE is not set")
        return self._code

    async def builder_status(self) -> Any:
        code = self._require_code()
        out: dict[str, Any] = {"builder_code": code}
        if self._clients.has_trader:
            client = await self._clients.secure()
            rates = await client.get_builder_fee_rates(code)
            # The SDK returns fractions of notional; show basis points.
            out["maker_fee_bps"] = str(rates.maker * 10_000)
            out["taker_fee_bps"] = str(rates.taker * 10_000)
        else:
            out["fee_rates"] = "set POLY_PRIVATE_KEY to read the configured fee rates"
        return out

    async def approve_builder(self, max_fee_bps: Decimal) -> Any:
        raise VenueError(
            "Prediction-market builder fees need no trader approval: the builder code is "
            "part of each signed order."
        )

    async def builder_earnings(self) -> Any:
        code = self._require_code()
        page = await self._clients.public().list_builder_trades(builder_code=code).first_page()
        trades = to_jsonable(page.items)
        volume = sum((t.size_usdc for t in page.items), Decimal(0))
        fees = sum((t.fee_usdc for t in page.items), Decimal(0))
        return {
            "builder_code": code,
            "matched_trades_on_page": len(trades),
            "volume_usdc_on_page": str(volume),
            "fee_usdc_on_page": str(fees),
            "has_more": page.has_more,
            "trades": trades,
        }
