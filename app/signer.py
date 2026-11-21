"""Signer abstraction.

Venue adapters ask a Signer for key material instead of reading the environment
themselves. Today there is one implementation, backed by a key in .env on this
machine. When this becomes a multi-user app, users sign in their own wallets on
the client and the server holds no trader keys; adapters then take a different
Signer (or pre-signed payloads) without changing their trading logic.
"""

from __future__ import annotations

from typing import Protocol

from eth_account import Account
from eth_account.signers.local import LocalAccount


class Signer(Protocol):
    @property
    def address(self) -> str: ...

    def private_key(self) -> str: ...

    def account(self) -> LocalAccount: ...


class EnvKeySigner:
    """Signer for a private key supplied through configuration."""

    def __init__(self, private_key: str) -> None:
        self._key = private_key
        self._account: LocalAccount = Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self._account.address

    def private_key(self) -> str:
        return self._key

    def account(self) -> LocalAccount:
        return self._account

    def __repr__(self) -> str:  # never print the key
        return f"EnvKeySigner({self.address})"
