"""Shared venue interface and helpers."""

from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class VenueError(Exception):
    """The venue rejected a request or the request was invalid."""


class NotConfigured(VenueError):
    """A required key, address or builder identity is missing from .env."""


@dataclass(frozen=True)
class OrderRequest:
    venue: str
    symbol: str  # CLOB: token id. Perps: instrument id. Hyperliquid: coin, e.g. "BTC".
    side: str  # BUY | SELL
    size: Decimal  # shares / contracts / base units
    price: Decimal | None = None  # None = market
    tif: str = "gtc"  # gtc | ioc | fok
    post_only: bool = False
    reduce_only: bool = False


@dataclass
class OrderResult:
    status: str  # open | filled | rejected | dry_run
    venue_order_id: str | None = None
    builder: str | None = None
    builder_fee_bps: str | None = None
    error: str | None = None
    raw: Any = None


def to_jsonable(value: Any) -> Any:
    """Turn SDK models (pydantic, dataclasses, Decimals, datetimes) into plain JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    return str(value)


class Venue(ABC):
    name: str

    # --- public market data -------------------------------------------------
    @abstractmethod
    async def markets(self, query: str | None = None, limit: int = 20) -> Any: ...

    @abstractmethod
    async def orderbook(self, symbol: str) -> Any: ...

    @abstractmethod
    async def reference_price(self, symbol: str, side: str) -> Decimal:
        """Price used to estimate the notional of a market order."""

    # --- trader account -----------------------------------------------------
    @abstractmethod
    def trader_address(self) -> str | None: ...

    @abstractmethod
    async def account(self) -> Any:
        """Balances, positions and open orders for the trader."""

    @abstractmethod
    async def place_order(self, req: OrderRequest) -> OrderResult: ...

    @abstractmethod
    async def cancel(self, symbol: str, order_id: str) -> Any: ...

    @abstractmethod
    async def fills(self) -> list[dict]:
        """Recent fills, normalised to the fills table columns."""

    # --- builder ------------------------------------------------------------
    @abstractmethod
    def builder_identity(self) -> dict:
        """The builder id and fee attached to orders on this venue."""

    @abstractmethod
    async def builder_status(self) -> Any: ...

    @abstractmethod
    async def approve_builder(self, max_fee_bps: Decimal) -> Any:
        """Trader approves the builder fee. max_fee_bps=0 revokes."""

    @abstractmethod
    async def builder_earnings(self) -> Any: ...

    async def close(self) -> None:
        return None
