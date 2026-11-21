"""Stand-ins for the venue SDKs. No test touches the network."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from app.venues.base import OrderRequest, OrderResult, Venue

BUILDER = "0x" + "b" * 40
TRADER = "0x" + "a" * 40
# Well-known throwaway test key (Hardhat account #0). Never holds real funds.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


class Page:
    def __init__(self, items, has_more=False):
        self.items = tuple(items)
        self.has_more = has_more


class Paginator:
    def __init__(self, items):
        self._items = items

    async def first_page(self):
        return Page(self._items)


# --- Hyperliquid -------------------------------------------------------------
class FakeHLInfo:
    def __init__(self, approved_tenths=0, builder_value="150.0"):
        self.approved_tenths = approved_tenths
        self.builder_value = builder_value
        self.posts: list[tuple] = []

    def meta(self):
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]}

    def all_mids(self):
        return {"BTC": "100000.0", "ETH": "4000.0"}

    def l2_snapshot(self, coin):
        return {"coin": coin, "time": 1, "levels": [[{"px": "99999", "sz": "1", "n": 1}], [{"px": "100001", "sz": "2", "n": 1}]]}

    def user_state(self, address):
        return {"marginSummary": {"accountValue": self.builder_value}, "withdrawable": "10", "assetPositions": []}

    def open_orders(self, address):
        return []

    def user_fills(self, address):
        return [{"coin": "BTC", "px": "100000", "sz": "0.0001", "side": "B", "time": 1700000000000, "oid": 7, "tid": 99, "fee": "0.004", "builderFee": "0.001"}]

    def post(self, path, payload):
        self.posts.append((path, payload))
        return self.approved_tenths

    def query_referral_state(self, user):
        return {"builderRewards": "1.23", "unclaimedRewards": "0.5", "claimedRewards": "0.73"}


class FakeHLExchange:
    def __init__(self, response=None):
        self.calls: list[tuple] = []
        self.response = response or {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 42}}]}}}

    def order(self, name, is_buy, sz, limit_px, order_type, reduce_only=False, cloid=None, builder=None):
        self.calls.append(("order", name, is_buy, sz, limit_px, order_type, reduce_only, builder))
        return self.response

    def market_open(self, name, is_buy, sz, px=None, slippage=0.05, cloid=None, builder=None):
        self.calls.append(("market_open", name, is_buy, sz, builder))
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"oid": 43, "totalSz": str(sz), "avgPx": "100000"}}]}}}

    def market_close(self, coin, sz=None, px=None, slippage=0.05, cloid=None, builder=None):
        self.calls.append(("market_close", coin, sz, builder))
        return None

    def cancel(self, name, oid):
        self.calls.append(("cancel", name, oid))
        return {"status": "ok"}

    def approve_builder_fee(self, builder, max_fee_rate):
        self.calls.append(("approve", builder, max_fee_rate))
        return {"status": "ok", "response": {"type": "default"}}


# --- Polymarket --------------------------------------------------------------
class Model:
    """Minimal pydantic-like object."""

    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, mode="json", include=None):
        out = {}
        for k, v in self.__dict__.items():
            if include and k not in include:
                continue
            if isinstance(v, Decimal):
                v = str(v)
            elif isinstance(v, datetime):
                v = v.isoformat()
            elif isinstance(v, Model):
                v = v.model_dump()
            out[k] = v
        return out


def builder_status(active=True):
    return Model(address=BUILDER, registered=active, enabled=active, admission_enabled=active, max_fee_rate=Decimal("0.001"))


class FakePerpsSession:
    def __init__(self, builder_attribution):
        self.selected_builder = builder_attribution
        self.closed = False
        self.placed: list[dict] = []
        self.approvals: list[tuple] = []
        self.builder_attribution = (
            Model(address=builder_attribution, fee_rate=Decimal("0.0003")) if builder_attribution else None
        )

    async def place_order(self, **kw):
        self.placed.append(kw)
        order = Model(id=555, status="open", builder=self.builder_attribution, instrument_id=kw["instrument_id"])
        return SimpleNamespace(order=order, tp_sl=None)

    async def cancel_order(self, *, order_id):
        return Model(status="ok", order_id=order_id, client_order_id=None)

    async def approve_builder_fee(self, *, builder_address=None, max_fee_rate):
        self.approvals.append((builder_address, max_fee_rate))
        return Model(trader=TRADER, builder=builder_address, max_fee_rate=Decimal(max_fee_rate), approval_version=1)

    async def revoke_builder_fee(self, builder_address=None):
        self.approvals.append((builder_address, "0"))
        return Model(trader=TRADER, builder=builder_address, max_fee_rate=Decimal("0"), approval_version=2)

    async def fetch_builder_approvals(self, *, builder=None):
        return (Model(trader=TRADER, builder=builder, max_fee_rate=Decimal("0.0003"), approval_version=1),)

    def list_fills(self):
        ts = datetime(2026, 11, 1, tzinfo=timezone.utc)
        return Paginator([Model(trade_id=1, order_id=555, instrument_id=3, side="long", price=Decimal("50"), quantity=Decimal("0.1"), fee=Decimal("0.002"), builder_fee=Decimal("0.0015"), timestamp=ts)])

    async def fetch_balances(self):
        return (Model(asset="pUSD", balance=Decimal("10"), value=Decimal("10")),)

    async def fetch_portfolio(self):
        return Model(positions=(), withdrawable=Decimal("10"))

    async def fetch_open_orders(self):
        return ()

    async def close(self):
        self.closed = True


class FakeSecureClient:
    wallet = TRADER
    signer = TRADER

    def __init__(self):
        self.limit_orders: list[dict] = []
        self.market_orders: list[dict] = []
        self.sessions: list[FakePerpsSession] = []

    async def place_limit_order(self, **kw):
        self.limit_orders.append(kw)
        return Model(ok=True, order_id="0xorder", status="live")

    async def place_market_order(self, **kw):
        self.market_orders.append(kw)
        return Model(ok=True, order_id="0xmkt", status="matched")

    async def cancel_order(self, *, order_id):
        return Model(canceled=(order_id,), not_canceled={})

    async def get_builder_fee_rates(self, code):
        return Model(maker=Decimal("0.0005"), taker=Decimal("0.001"))

    async def open_perps_session(self, *, builder_attribution=None, label=None, **kw):
        session = FakePerpsSession(builder_attribution)
        self.sessions.append(session)
        return session


class FakePublicClient:
    def __init__(self, active=True):
        self.active = active

    async def fetch_perps_builder_status(self, *, address):
        return builder_status(self.active)

    async def fetch_perps_ticker(self, *, instrument_id):
        return Model(instrument_id=instrument_id, mark_price=Decimal("50"))

    async def get_price(self, *, token_id, side):
        return Decimal("0.5")

    def list_builder_trades(self, *, builder_code):
        return Paginator([Model(id="t1", size_usdc=Decimal("10"), fee_usdc=Decimal("0.05"))])


class FakeClients:
    """Replaces PolymarketClients."""

    def __init__(self, active=True, has_trader=True):
        self.secure_client = FakeSecureClient()
        self.public_client = FakePublicClient(active)
        self.has_trader = has_trader

    def public(self):
        return self.public_client

    async def secure(self):
        return self.secure_client

    def wallet_if_known(self):
        return TRADER


# --- generic venue for service / API tests ----------------------------------
@dataclass
class FakeVenue(Venue):
    name: str = "fake"
    price: Decimal = Decimal("10")
    placed: list[OrderRequest] = field(default_factory=list)
    approved: list[Decimal] = field(default_factory=list)

    async def markets(self, query=None, limit=20):
        return [{"symbol": "X"}]

    async def orderbook(self, symbol):
        return {"symbol": symbol, "bids": [], "asks": []}

    async def reference_price(self, symbol, side):
        return self.price

    def trader_address(self):
        return TRADER

    async def account(self):
        return {"address": TRADER}

    async def place_order(self, req):
        self.placed.append(req)
        return OrderResult(status="open", venue_order_id="1", builder=BUILDER, builder_fee_bps="1")

    async def cancel(self, symbol, order_id):
        return {"cancelled": order_id}

    async def fills(self):
        return [{"fill_id": "f1", "order_id": "1", "symbol": "X", "side": "BUY", "price": "10", "size": "1", "fee": "0.01", "builder_fee": "0.001", "ts": 1, "raw": {}}]

    def builder_identity(self):
        return {"builder": BUILDER, "fee_bps": "1"}

    async def builder_status(self):
        return {"builder": BUILDER}

    async def approve_builder(self, max_fee_bps):
        self.approved.append(max_fee_bps)
        return {"ok": True}

    async def builder_earnings(self):
        return {"total": "0"}
