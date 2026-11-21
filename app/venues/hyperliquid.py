"""Hyperliquid adapter.

Builder codes on Hyperliquid are just the builder's wallet address. The trader
approves a max fee for that address once (signed by the trader's MAIN wallet,
not an agent wallet), then every order carries {"b": address, "f": fee} where
f is in tenths of a basis point.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from app.signer import Signer
from app.venues.base import NotConfigured, OrderRequest, OrderResult, Venue, VenueError

MAINNET_URL = "https://api.hyperliquid.xyz"
TESTNET_URL = "https://api.hyperliquid-testnet.xyz"
MIN_BUILDER_ACCOUNT_VALUE = Decimal("100")  # USDC in perps account value


def bps_to_tenths(bps: Decimal) -> int:
    """Order field `f`: tenths of a basis point (10 = 1 bp)."""
    return int(bps * 10)


def bps_to_percent_string(bps: Decimal) -> str:
    """approveBuilderFee wants a percent string: 1 bp -> '0.01%'."""
    return f"{format((bps / Decimal(100)).normalize(), 'f')}%"


def parse_order_response(resp: Any) -> OrderResult:
    if not isinstance(resp, dict) or resp.get("status") != "ok":
        return OrderResult(status="rejected", error=str(resp.get("response") if isinstance(resp, dict) else resp), raw=resp)
    statuses = (resp.get("response", {}).get("data", {}) or {}).get("statuses") or []
    if not statuses:
        return OrderResult(status="rejected", error="empty response", raw=resp)
    first = statuses[0]
    if isinstance(first, dict) and "error" in first:
        return OrderResult(status="rejected", error=str(first["error"]), raw=resp)
    if isinstance(first, dict) and "resting" in first:
        return OrderResult(status="open", venue_order_id=str(first["resting"]["oid"]), raw=resp)
    if isinstance(first, dict) and "filled" in first:
        return OrderResult(status="filled", venue_order_id=str(first["filled"]["oid"]), raw=resp)
    return OrderResult(status="open", raw=resp)


def normalize_fill(f: dict) -> dict:
    return {
        "fill_id": str(f.get("tid")),
        "order_id": str(f.get("oid")) if f.get("oid") is not None else None,
        "symbol": f.get("coin"),
        "side": "BUY" if f.get("side") == "B" else "SELL",
        "price": str(f.get("px")),
        "size": str(f.get("sz")),
        "fee": str(f.get("fee")) if f.get("fee") is not None else None,
        "builder_fee": str(f["builderFee"]) if f.get("builderFee") is not None else None,
        "ts": f.get("time"),
        "raw": f,
    }


class HyperliquidVenue(Venue):
    name = "hyperliquid"

    def __init__(
        self,
        *,
        network: str,
        trader: Signer | None,
        account_address: str | None,
        builder_address: str | None,
        builder_fee_bps: Decimal,
        info_factory=None,
        exchange_factory=None,
    ) -> None:
        self._base_url = MAINNET_URL if network == "mainnet" else TESTNET_URL
        self._network = network
        self._trader = trader
        self._account_address = account_address
        self._builder = builder_address.lower() if builder_address else None
        self._fee_bps = builder_fee_bps
        self._info_factory = info_factory
        self._exchange_factory = exchange_factory
        self._info = None
        self._exchange = None

    # SDK objects are created lazily: their constructors call the network.
    def _get_info(self):
        if self._info is None:
            if self._info_factory:
                self._info = self._info_factory()
            else:
                from hyperliquid.info import Info

                self._info = Info(self._base_url, skip_ws=True)
        return self._info

    def _get_exchange(self):
        if self._trader is None:
            raise NotConfigured("HL_PRIVATE_KEY is not set")
        if self._exchange is None:
            if self._exchange_factory:
                self._exchange = self._exchange_factory()
            else:
                from hyperliquid.exchange import Exchange

                self._exchange = Exchange(
                    self._trader.account(), self._base_url, account_address=self._account_address
                )
        return self._exchange

    def trader_address(self) -> str | None:
        if self._account_address:
            return self._account_address
        return self._trader.address if self._trader else None

    def _require_trader(self) -> str:
        addr = self.trader_address()
        if not addr:
            raise NotConfigured("HL_PRIVATE_KEY (or HL_ACCOUNT_ADDRESS) is not set")
        return addr

    def _builder_info(self) -> dict | None:
        if not self._builder:
            return None
        return {"b": self._builder, "f": bps_to_tenths(self._fee_bps)}

    # --- market data --------------------------------------------------------
    async def markets(self, query: str | None = None, limit: int = 20) -> Any:
        info = self._get_info()
        meta, mids = await asyncio.gather(
            asyncio.to_thread(info.meta), asyncio.to_thread(info.all_mids)
        )
        out = []
        for asset in meta.get("universe", []):
            name = asset.get("name")
            if query and query.lower() not in str(name).lower():
                continue
            out.append(
                {
                    "title": name,
                    "subtitle": f"Perpetual, up to {asset.get('maxLeverage')}x",
                    "legs": [{"label": name, "symbol": name, "price": mids.get(name)}],
                    "symbol": name,
                    "mid": mids.get(name),
                    "size_decimals": asset.get("szDecimals"),
                    "max_leverage": asset.get("maxLeverage"),
                }
            )
            if len(out) >= limit:
                break
        return out

    async def orderbook(self, symbol: str) -> Any:
        book = await asyncio.to_thread(self._get_info().l2_snapshot, symbol)
        levels = book.get("levels") or [[], []]
        return {
            "symbol": symbol,
            "bids": [{"price": l["px"], "size": l["sz"]} for l in levels[0]],
            "asks": [{"price": l["px"], "size": l["sz"]} for l in levels[1]],
            "time": book.get("time"),
        }

    async def reference_price(self, symbol: str, side: str) -> Decimal:
        mids = await asyncio.to_thread(self._get_info().all_mids)
        if symbol not in mids:
            raise VenueError(f"unknown Hyperliquid coin {symbol!r}")
        return Decimal(str(mids[symbol]))

    # --- account ------------------------------------------------------------
    async def account(self) -> Any:
        addr = self._require_trader()
        info = self._get_info()
        state, orders = await asyncio.gather(
            asyncio.to_thread(info.user_state, addr), asyncio.to_thread(info.open_orders, addr)
        )
        return {
            "address": addr,
            "network": self._network,
            "margin_summary": state.get("marginSummary"),
            "withdrawable": state.get("withdrawable"),
            "positions": [p.get("position") for p in state.get("assetPositions", [])],
            "open_orders": orders,
        }

    async def place_order(self, req: OrderRequest) -> OrderResult:
        exchange = self._get_exchange()
        builder = self._builder_info()
        is_buy = req.side == "BUY"
        size = float(req.size)

        if req.price is None:
            if req.reduce_only:
                resp = await asyncio.to_thread(exchange.market_close, req.symbol, size, builder=builder)
            else:
                resp = await asyncio.to_thread(exchange.market_open, req.symbol, is_buy, size, builder=builder)
        else:
            if req.tif == "fok":
                raise VenueError("Hyperliquid does not support FOK; use gtc or ioc")
            tif = "Alo" if req.post_only else {"gtc": "Gtc", "ioc": "Ioc"}[req.tif]
            resp = await asyncio.to_thread(
                exchange.order,
                req.symbol,
                is_buy,
                size,
                float(req.price),
                {"limit": {"tif": tif}},
                req.reduce_only,
                builder=builder,
            )
        if resp is None:
            raise VenueError("no response from Hyperliquid (no position to close?)")
        result = parse_order_response(resp)
        if builder:
            result.builder = builder["b"]
            result.builder_fee_bps = str(self._fee_bps)
        return result

    async def cancel(self, symbol: str, order_id: str) -> Any:
        return await asyncio.to_thread(self._get_exchange().cancel, symbol, int(order_id))

    async def fills(self) -> list[dict]:
        addr = self._require_trader()
        raw = await asyncio.to_thread(self._get_info().user_fills, addr)
        return [normalize_fill(f) for f in raw]

    # --- builder ------------------------------------------------------------
    def builder_identity(self) -> dict:
        return {
            "builder": self._builder,
            "fee_bps": str(self._fee_bps),
            "order_field": self._builder_info(),
            "network": self._network,
        }

    async def builder_status(self) -> Any:
        if not self._builder:
            raise NotConfigured("HL_BUILDER_ADDRESS is not set")
        info = self._get_info()
        state = await asyncio.to_thread(info.user_state, self._builder)
        value = Decimal(str((state.get("marginSummary") or {}).get("accountValue", "0")))
        out: dict[str, Any] = {
            "builder": self._builder,
            "network": self._network,
            "builder_account_value": str(value),
            "builder_eligible": value >= MIN_BUILDER_ACCOUNT_VALUE,
            "order_fee_bps": str(self._fee_bps),
        }
        trader = self.trader_address()
        if trader:
            approved_tenths = await asyncio.to_thread(
                info.post, "/info", {"type": "maxBuilderFee", "user": trader, "builder": self._builder}
            )
            approved_bps = Decimal(int(approved_tenths or 0)) / 10
            out["trader"] = trader
            out["trader_approved_max_fee_bps"] = str(approved_bps)
            out["approval_sufficient"] = approved_bps >= self._fee_bps and (
                approved_bps > 0 or self._fee_bps == 0
            )
        return out

    async def approve_builder(self, max_fee_bps: Decimal) -> Any:
        if not self._builder:
            raise NotConfigured("HL_BUILDER_ADDRESS is not set")
        resp = await asyncio.to_thread(
            self._get_exchange().approve_builder_fee, self._builder, bps_to_percent_string(max_fee_bps)
        )
        if isinstance(resp, dict) and resp.get("status") != "ok":
            raise VenueError(f"approval rejected: {resp.get('response')}")
        return resp

    async def builder_earnings(self) -> Any:
        if not self._builder:
            raise NotConfigured("HL_BUILDER_ADDRESS is not set")
        state = await asyncio.to_thread(self._get_info().query_referral_state, self._builder)
        return {
            "builder": self._builder,
            "builder_rewards": state.get("builderRewards"),
            "unclaimed_rewards": state.get("unclaimedRewards"),
            "claimed_rewards": state.get("claimedRewards"),
            "note": "Builder fees are claimed through Hyperliquid's referral reward claim.",
        }
