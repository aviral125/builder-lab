"""Shared Polymarket SDK clients (one public, one authenticated for the trader).

Both the prediction-market adapter and the perps adapter use the same
`polymarket-client` SDK, so they share clients. Clients are created on first use
because creating the authenticated one calls the network.
"""

from __future__ import annotations

import asyncio

from app.signer import Signer
from app.venues.base import NotConfigured


class PolymarketClients:
    def __init__(self, trader: Signer | None, wallet_address: str | None) -> None:
        self._trader = trader
        self._wallet = wallet_address
        self._public = None
        self._secure = None
        self._lock = asyncio.Lock()

    @property
    def has_trader(self) -> bool:
        return self._trader is not None

    def public(self):
        if self._public is None:
            from polymarket import AsyncPublicClient

            self._public = AsyncPublicClient()
        return self._public

    async def secure(self):
        if self._trader is None:
            raise NotConfigured("POLY_PRIVATE_KEY is not set")
        async with self._lock:
            if self._secure is None:
                from polymarket import AsyncSecureClient

                self._secure = await AsyncSecureClient.create(
                    private_key=self._trader.private_key(), wallet=self._wallet
                )
        return self._secure

    def wallet_if_known(self) -> str | None:
        """Wallet address without forcing a network call."""
        if self._secure is not None:
            return str(self._secure.wallet)
        return self._wallet

    async def close(self) -> None:
        for client in (self._secure, self._public):
            if client is not None:
                try:
                    await client.close()
                except Exception:
                    pass
        self._secure = None
        self._public = None
