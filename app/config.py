"""Settings loaded from the environment (.env). Secrets never leave this module's
callers: nothing here is logged or returned by the API."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from dotenv import load_dotenv


class ConfigError(Exception):
    pass


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _decimal(name: str, default: str) -> Decimal:
    raw = (os.getenv(name) or "").strip() or default
    try:
        return Decimal(raw)
    except InvalidOperation as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _str(name: str) -> str | None:
    raw = (os.getenv(name) or "").strip()
    return raw or None


def _address(name: str) -> str | None:
    raw = _str(name)
    if raw is None:
        return None
    if not (raw.startswith("0x") and len(raw) == 42):
        raise ConfigError(f"{name} must be a 0x-prefixed 20-byte address")
    return raw


@dataclass(frozen=True)
class Settings:
    dry_run: bool
    max_order_usd: Decimal
    host: str
    port: int
    db_path: str
    poll_seconds: int

    poly_private_key: str | None
    poly_wallet_address: str | None
    poly_builder_code: str | None

    perps_builder_address: str | None
    perps_builder_fee_bps: Decimal
    perps_builder_private_key: str | None

    hl_network: str
    hl_private_key: str | None
    hl_account_address: str | None
    hl_builder_address: str | None
    hl_builder_fee_bps: Decimal

    def public_view(self) -> dict:
        """Config that is safe to show in the UI. No keys."""
        return {
            "dry_run": self.dry_run,
            "max_order_usd": str(self.max_order_usd),
            "polymarket": {
                "trader_key_set": bool(self.poly_private_key),
                "builder_code": self.poly_builder_code,
            },
            "polymarket_perps": {
                "trader_key_set": bool(self.poly_private_key),
                "builder_address": self.perps_builder_address,
                "builder_fee_bps": str(self.perps_builder_fee_bps),
                "builder_key_set": bool(self.perps_builder_private_key),
            },
            "hyperliquid": {
                "network": self.hl_network,
                "trader_key_set": bool(self.hl_private_key),
                "builder_address": self.hl_builder_address,
                "builder_fee_bps": str(self.hl_builder_fee_bps),
            },
        }


def load_settings(env_file: str | None = ".env") -> Settings:
    if env_file:
        load_dotenv(env_file)

    network = (_str("HL_NETWORK") or "testnet").lower()
    if network not in ("testnet", "mainnet"):
        raise ConfigError("HL_NETWORK must be 'testnet' or 'mainnet'")

    code = _str("POLY_BUILDER_CODE")
    if code is not None and not (code.startswith("0x") and len(code) == 66):
        raise ConfigError("POLY_BUILDER_CODE must be a 0x-prefixed 32-byte hex string")

    hl_fee = _decimal("HL_BUILDER_FEE_BPS", "0")
    perps_fee = _decimal("POLY_PERPS_BUILDER_FEE_BPS", "0")
    for name, fee in (("HL_BUILDER_FEE_BPS", hl_fee), ("POLY_PERPS_BUILDER_FEE_BPS", perps_fee)):
        if fee < 0 or fee > 10:
            raise ConfigError(f"{name} must be between 0 and 10 (venue cap on perps is 10 bps)")
    # Hyperliquid encodes the fee in tenths of a basis point.
    if (hl_fee * 10) % 1 != 0:
        raise ConfigError("HL_BUILDER_FEE_BPS supports at most one decimal place")

    max_usd = _decimal("MAX_ORDER_USD", "25")
    if max_usd <= 0:
        raise ConfigError("MAX_ORDER_USD must be positive")

    host = _str("HOST") or "127.0.0.1"

    return Settings(
        dry_run=_bool("DRY_RUN", True),
        max_order_usd=max_usd,
        host=host,
        port=int(_str("PORT") or "8000"),
        db_path=_str("DB_PATH") or "builder_lab.db",
        poll_seconds=max(5, int(_str("POLL_SECONDS") or "30")),
        poly_private_key=_str("POLY_PRIVATE_KEY"),
        poly_wallet_address=_address("POLY_WALLET_ADDRESS"),
        poly_builder_code=code,
        perps_builder_address=_address("POLY_PERPS_BUILDER_ADDRESS"),
        perps_builder_fee_bps=perps_fee,
        perps_builder_private_key=_str("POLY_PERPS_BUILDER_PRIVATE_KEY"),
        hl_network=network,
        hl_private_key=_str("HL_PRIVATE_KEY"),
        hl_account_address=_address("HL_ACCOUNT_ADDRESS"),
        hl_builder_address=_address("HL_BUILDER_ADDRESS"),
        hl_builder_fee_bps=hl_fee,
    )
