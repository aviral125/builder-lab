from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.config import ConfigError, load_settings
from app.db import Database
from app.main import create_app
from app.service import TradingService, build_venues
from app.signer import EnvKeySigner
from app.venues.base import NotConfigured, OrderRequest, VenueError, to_jsonable
from app.venues import hyperliquid as hl
from app.venues.polymarket_clob import PolymarketClobVenue
from app.venues.polymarket_perps import PolymarketPerpsVenue, bps_to_rate
from tests.fakes import (
    BUILDER,
    TEST_KEY,
    TRADER,
    FakeClients,
    FakeHLExchange,
    FakeHLInfo,
    FakeVenue,
)

CODE = "0x" + "c" * 64
ENV_VARS = [
    "DRY_RUN", "MAX_ORDER_USD", "HOST", "PORT", "DB_PATH", "POLL_SECONDS", "POLY_PRIVATE_KEY",
    "POLY_WALLET_ADDRESS", "POLY_BUILDER_CODE", "POLY_PERPS_BUILDER_ADDRESS",
    "POLY_PERPS_BUILDER_FEE_BPS", "POLY_PERPS_BUILDER_PRIVATE_KEY", "HL_NETWORK",
    "HL_PRIVATE_KEY", "HL_ACCOUNT_ADDRESS", "HL_BUILDER_ADDRESS", "HL_BUILDER_FEE_BPS",
]


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def settings(env, **overrides):
    for k, v in overrides.items():
        env.setenv(k, v)
    return load_settings(env_file=None)


# --- config ------------------------------------------------------------------
def test_defaults_are_safe(env):
    s = settings(env)
    assert s.dry_run is True
    assert s.host == "127.0.0.1"
    assert s.hl_network == "testnet"
    assert s.max_order_usd == Decimal("25")


def test_public_view_has_no_keys(env):
    s = settings(env, POLY_PRIVATE_KEY=TEST_KEY, HL_PRIVATE_KEY=TEST_KEY)
    assert TEST_KEY not in str(s.public_view())
    assert s.public_view()["hyperliquid"]["trader_key_set"] is True


@pytest.mark.parametrize(
    "name,value",
    [
        ("HL_NETWORK", "devnet"),
        ("POLY_BUILDER_CODE", "0x1234"),
        ("HL_BUILDER_ADDRESS", "not-an-address"),
        ("HL_BUILDER_FEE_BPS", "11"),
        ("HL_BUILDER_FEE_BPS", "0.25"),
        ("POLY_PERPS_BUILDER_FEE_BPS", "-1"),
        ("MAX_ORDER_USD", "0"),
        ("MAX_ORDER_USD", "abc"),
    ],
)
def test_bad_config_is_rejected(env, name, value):
    with pytest.raises(ConfigError):
        settings(env, **{name: value})


def test_build_venues_rejects_same_perps_builder_and_trader(env):
    s = settings(env, POLY_PRIVATE_KEY=TEST_KEY, POLY_PERPS_BUILDER_PRIVATE_KEY=TEST_KEY)
    with pytest.raises(VenueError):
        build_venues(s)


def test_signer_repr_hides_key():
    signer = EnvKeySigner(TEST_KEY)
    assert TEST_KEY not in repr(signer)
    assert signer.address == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"


# --- hyperliquid -------------------------------------------------------------
def test_hl_fee_units():
    assert hl.bps_to_tenths(Decimal("1")) == 10
    assert hl.bps_to_tenths(Decimal("2.5")) == 25
    assert hl.bps_to_percent_string(Decimal("1")) == "0.01%"
    assert hl.bps_to_percent_string(Decimal("10")) == "0.1%"
    assert hl.bps_to_percent_string(Decimal("0")) == "0%"


def test_hl_parse_order_response():
    ok = {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 5}}]}}}
    assert hl.parse_order_response(ok).status == "open"
    assert hl.parse_order_response(ok).venue_order_id == "5"
    filled = {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 6}}]}}}
    assert hl.parse_order_response(filled).status == "filled"
    err = {"status": "ok", "response": {"data": {"statuses": [{"error": "Builder fee has not been approved."}]}}}
    r = hl.parse_order_response(err)
    assert r.status == "rejected" and "approved" in r.error
    assert hl.parse_order_response({"status": "err", "response": "bad"}).status == "rejected"


def make_hl(fee="1", exchange=None, info=None, builder=BUILDER.upper().replace("0X", "0x")):
    exchange = exchange or FakeHLExchange()
    info = info or FakeHLInfo()
    venue = hl.HyperliquidVenue(
        network="testnet",
        trader=EnvKeySigner(TEST_KEY),
        account_address=None,
        builder_address=builder,
        builder_fee_bps=Decimal(fee),
        info_factory=lambda: info,
        exchange_factory=lambda: exchange,
    )
    return venue, exchange, info


def test_hl_limit_order_carries_builder():
    venue, exchange, _ = make_hl(fee="1.5")
    req = OrderRequest(venue="hyperliquid", symbol="BTC", side="BUY", size=Decimal("0.0001"), price=Decimal("90000"))
    result = run(venue.place_order(req))
    call = exchange.calls[0]
    assert call[0] == "order" and call[5] == {"limit": {"tif": "Gtc"}}
    assert call[-1] == {"b": BUILDER, "f": 15}  # lowercased address, tenths of a bp
    assert result.status == "open" and result.venue_order_id == "42"
    assert result.builder == BUILDER and result.builder_fee_bps == "1.5"


def test_hl_market_and_post_only_and_fok():
    venue, exchange, _ = make_hl()
    run(venue.place_order(OrderRequest(venue="hyperliquid", symbol="BTC", side="SELL", size=Decimal("0.0001"), tif="ioc")))
    assert exchange.calls[-1][0] == "market_open" and exchange.calls[-1][2] is False
    run(venue.place_order(OrderRequest(venue="hyperliquid", symbol="BTC", side="BUY", size=Decimal("0.0001"), price=Decimal("1"), post_only=True)))
    assert exchange.calls[-1][5] == {"limit": {"tif": "Alo"}}
    with pytest.raises(VenueError):
        run(venue.place_order(OrderRequest(venue="hyperliquid", symbol="BTC", side="BUY", size=Decimal("1"), price=Decimal("1"), tif="fok")))


def test_hl_without_builder_sends_no_builder_field():
    venue, exchange, _ = make_hl(builder=None)
    run(venue.place_order(OrderRequest(venue="hyperliquid", symbol="BTC", side="BUY", size=Decimal("0.0001"), price=Decimal("1"))))
    assert exchange.calls[0][-1] is None
    with pytest.raises(NotConfigured):
        run(venue.builder_status())


def test_hl_approve_and_status_and_earnings():
    venue, exchange, info = make_hl(fee="1", info=FakeHLInfo(approved_tenths=10))
    run(venue.approve_builder(Decimal("1")))
    assert exchange.calls[-1] == ("approve", BUILDER, "0.01%")
    status = run(venue.builder_status())
    assert status["builder_eligible"] is True
    assert status["trader_approved_max_fee_bps"] == "1"
    assert status["approval_sufficient"] is True
    assert info.posts[0][1]["type"] == "maxBuilderFee"
    assert run(venue.builder_earnings())["builder_rewards"] == "1.23"


def test_hl_status_flags_insufficient_approval_and_low_balance():
    venue, _, _ = make_hl(fee="2", info=FakeHLInfo(approved_tenths=10, builder_value="50"))
    status = run(venue.builder_status())
    assert status["builder_eligible"] is False
    assert status["approval_sufficient"] is False


def test_hl_fills_and_book_and_markets():
    venue, _, _ = make_hl()
    fill = run(venue.fills())[0]
    assert fill["fill_id"] == "99" and fill["side"] == "BUY" and fill["builder_fee"] == "0.001"
    book = run(venue.orderbook("BTC"))
    assert book["bids"][0] == {"price": "99999", "size": "1"}
    assert [m["symbol"] for m in run(venue.markets("eth"))] == ["ETH"]
    assert run(venue.reference_price("BTC", "BUY")) == Decimal("100000.0")
    with pytest.raises(VenueError):
        run(venue.reference_price("NOPE", "BUY"))


def test_hl_requires_trader_key():
    venue = hl.HyperliquidVenue(network="testnet", trader=None, account_address=None, builder_address=BUILDER, builder_fee_bps=Decimal(0), info_factory=FakeHLInfo)
    with pytest.raises(NotConfigured):
        run(venue.place_order(OrderRequest(venue="hyperliquid", symbol="BTC", side="BUY", size=Decimal("1"), price=Decimal("1"))))
    with pytest.raises(NotConfigured):
        run(venue.fills())


# --- polymarket prediction markets -------------------------------------------
def test_clob_orders_carry_builder_code():
    clients = FakeClients()
    venue = PolymarketClobVenue(clients, CODE)
    r = run(venue.place_order(OrderRequest(venue="polymarket", symbol="123", side="BUY", size=Decimal("5"), price=Decimal("0.4"))))
    assert clients.secure_client.limit_orders[0]["builder_code"] == CODE
    assert r.status == "open" and r.venue_order_id == "0xorder" and r.builder == CODE
    r = run(venue.place_order(OrderRequest(venue="polymarket", symbol="123", side="BUY", size=Decimal("5"), price=Decimal("0.6"), tif="fok")))
    sent = clients.secure_client.market_orders[0]
    assert sent["builder_code"] == CODE and sent["order_type"] == "FOK" and sent["max_price"] == Decimal("0.6")
    assert r.status == "filled"
    run(venue.place_order(OrderRequest(venue="polymarket", symbol="123", side="SELL", size=Decimal("5"), tif="ioc")))
    assert clients.secure_client.market_orders[1]["order_type"] == "FAK"


def test_clob_builder_status_and_earnings():
    venue = PolymarketClobVenue(FakeClients(), CODE)
    status = run(venue.builder_status())
    assert Decimal(status["taker_fee_bps"]) == Decimal("10") and Decimal(status["maker_fee_bps"]) == Decimal("5")
    earnings = run(venue.builder_earnings())
    assert earnings["volume_usdc_on_page"] == "10" and earnings["matched_trades_on_page"] == 1
    with pytest.raises(VenueError):
        run(venue.approve_builder(Decimal("1")))
    with pytest.raises(NotConfigured):
        run(PolymarketClobVenue(FakeClients(), None).builder_status())


# --- polymarket perps --------------------------------------------------------
def test_perps_rate_units():
    assert bps_to_rate(Decimal("3")) == "0.0003"
    assert bps_to_rate(Decimal("10")) == "0.001"


def test_perps_session_selects_builder_only_when_active():
    clients = FakeClients(active=True)
    venue = PolymarketPerpsVenue(clients, builder_address=BUILDER, builder_fee_bps=Decimal("3"))
    r = run(venue.place_order(OrderRequest(venue="polymarket_perps", symbol="3", side="BUY", size=Decimal("0.1"), price=Decimal("50"))))
    session = clients.secure_client.sessions[0]
    assert session.selected_builder == BUILDER
    assert session.placed[0] == {"instrument_id": 3, "side": "BUY", "quantity": Decimal("0.1"), "reduce_only": False, "time_in_force": "gtc", "price": Decimal("50"), "post_only": False}
    assert r.builder == BUILDER and Decimal(r.builder_fee_bps) == Decimal("3") and r.venue_order_id == "555"

    clients = FakeClients(active=False)
    venue = PolymarketPerpsVenue(clients, builder_address=BUILDER, builder_fee_bps=Decimal("3"))
    r = run(venue.place_order(OrderRequest(venue="polymarket_perps", symbol="3", side="SELL", size=Decimal("0.1"), tif="ioc")))
    assert clients.secure_client.sessions[0].selected_builder is None
    assert r.builder is None
    assert "price" not in clients.secure_client.sessions[0].placed[0]
    status = run(venue.builder_status())
    assert status["active"] is False and "note" in status


def test_perps_approve_revoke_and_fills():
    clients = FakeClients(active=True)
    venue = PolymarketPerpsVenue(clients, builder_address=BUILDER, builder_fee_bps=Decimal("3"))

    async def flow():
        await venue.approve_builder(Decimal("3"))
        await venue.approve_builder(Decimal("0"))
        return await venue.fills(), await venue.builder_status()

    fills, status = run(flow())
    session = clients.secure_client.sessions[0]
    assert session.approvals == [(BUILDER, "0.0003"), (BUILDER, "0")]
    assert fills[0]["side"] == "BUY" and fills[0]["builder_fee"] == "0.0015" and fills[0]["symbol"] == "3"
    assert Decimal(status["trader_approved_max_fee_bps"]) == Decimal("3")
    assert Decimal(status["venue_max_fee_bps"]) == Decimal("10")


def test_perps_earnings_needs_builder_key():
    venue = PolymarketPerpsVenue(FakeClients(), builder_address=BUILDER, builder_fee_bps=Decimal("3"))
    with pytest.raises(NotConfigured):
        run(venue.builder_earnings())


# --- service: safety rails and persistence -----------------------------------
def make_service(env, tmp_path, **overrides):
    s = settings(env, **overrides)
    venue = FakeVenue()
    service = TradingService(s, Database(str(tmp_path / "t.db")), {"fake": venue})
    return service, venue


def test_dry_run_never_reaches_venue(env, tmp_path):
    service, venue = make_service(env, tmp_path)
    row = run(service.place_order({"venue": "fake", "symbol": "X", "side": "buy", "size": "1", "price": "10"}))
    assert row["status"] == "dry_run" and venue.placed == []
    assert service.db.list_orders()[0]["dry_run"] == 1
    assert run(service.approve_builder("fake", "1"))["dry_run"] is True and venue.approved == []
    assert run(service.cancel("fake", "X", "1"))["dry_run"] is True


def test_max_order_cap_applies_to_limit_and_market(env, tmp_path):
    service, venue = make_service(env, tmp_path, DRY_RUN="false", MAX_ORDER_USD="25")
    with pytest.raises(VenueError, match="MAX_ORDER_USD"):
        run(service.place_order({"venue": "fake", "symbol": "X", "side": "BUY", "size": "3", "price": "10"}))
    with pytest.raises(VenueError, match="MAX_ORDER_USD"):  # market: 3 * reference price 10
        run(service.place_order({"venue": "fake", "symbol": "X", "side": "BUY", "size": "3"}))
    assert venue.placed == []
    row = run(service.place_order({"venue": "fake", "symbol": "X", "side": "BUY", "size": "2", "price": "10"}))
    assert row["status"] == "open" and len(venue.placed) == 1 and venue.placed[0].tif == "gtc"
    stored = service.db.list_orders("fake")[0]
    assert stored["builder"] == BUILDER and stored["trader"] == TRADER and stored["dry_run"] == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"venue": "nope", "symbol": "X", "side": "BUY", "size": "1", "price": "1"},
        {"venue": "fake", "symbol": "", "side": "BUY", "size": "1", "price": "1"},
        {"venue": "fake", "symbol": "X", "side": "HOLD", "size": "1", "price": "1"},
        {"venue": "fake", "symbol": "X", "side": "BUY", "size": "-1", "price": "1"},
        {"venue": "fake", "symbol": "X", "side": "BUY", "size": "abc", "price": "1"},
        {"venue": "fake", "symbol": "X", "side": "BUY", "size": "NaN", "price": "1"},
        {"venue": "fake", "symbol": "X", "side": "BUY", "size": "1", "tif": "gtc"},
        {"venue": "fake", "symbol": "X", "side": "BUY", "size": "1", "price": "1", "tif": "day"},
        {"venue": "fake", "symbol": "X", "side": "BUY", "size": "1", "post_only": True},
    ],
)
def test_invalid_orders_are_rejected(env, tmp_path, payload):
    service, venue = make_service(env, tmp_path, DRY_RUN="false")
    with pytest.raises(VenueError):
        run(service.place_order(payload))
    assert venue.placed == []


def test_sdk_exception_is_recorded_as_rejected(env, tmp_path):
    service, venue = make_service(env, tmp_path, DRY_RUN="false")

    async def boom(req):
        raise RuntimeError("socket closed")

    venue.place_order = boom
    row = run(service.place_order({"venue": "fake", "symbol": "X", "side": "BUY", "size": "1", "price": "1"}))
    assert row["status"] == "rejected" and "socket closed" in row["error"]


def test_approval_and_fill_sync_are_persisted(env, tmp_path):
    service, venue = make_service(env, tmp_path, DRY_RUN="false")
    run(service.approve_builder("fake", "2"))
    assert venue.approved == [Decimal("2")]
    assert service.db.list_approvals("fake")[0]["max_fee_bps"] == "2"
    with pytest.raises(VenueError):
        run(service.approve_builder("fake", "11"))
    assert run(service.sync_fills()) == {"fake": 1}
    assert run(service.sync_fills()) == {"fake": 0}  # idempotent
    assert service.db.list_fills("fake")[0]["builder_fee"] == "0.001"


def test_sync_skips_unconfigured_venue(env, tmp_path):
    service, venue = make_service(env, tmp_path)

    async def missing():
        raise NotConfigured("no key")

    venue.fills = missing
    assert run(service.sync_fills())["fake"].startswith("skipped")


def test_to_jsonable_handles_sdk_shapes():
    from datetime import datetime, timezone

    assert to_jsonable({"a": Decimal("1.5"), "b": (1, datetime(2026, 1, 1, tzinfo=timezone.utc))}) == {
        "a": "1.5",
        "b": [1, "2026-01-01T00:00:00+00:00"],
    }


# --- HTTP API ----------------------------------------------------------------
def make_client(env, tmp_path, **overrides):
    service, venue = make_service(env, tmp_path, **overrides)
    app = create_app(service.settings, service, start_poller=False)
    return TestClient(app), service, venue


def test_api_end_to_end(env, tmp_path):
    client, service, venue = make_client(env, tmp_path, DRY_RUN="false")
    with client:
        assert client.get("/").status_code == 200
        cfg = client.get("/api/config").json()
        assert cfg["dry_run"] is False and cfg["venues"] == ["fake"]
        assert client.get("/api/markets", params={"venue": "fake"}).json() == [{"symbol": "X"}]
        assert client.get("/api/book", params={"venue": "fake", "symbol": "X"}).status_code == 200
        r = client.post("/api/orders", json={"venue": "fake", "symbol": "X", "side": "BUY", "size": "1", "price": "10"})
        assert r.status_code == 200 and r.json()["status"] == "open"
        assert len(client.get("/api/orders").json()) == 1
        r = client.post("/api/orders", json={"venue": "fake", "symbol": "X", "side": "BUY", "size": "100", "price": "10"})
        assert r.status_code == 400 and r.json()["kind"] == "venue_error"
        assert client.get("/api/markets", params={"venue": "nope"}).status_code == 400
        assert client.delete("/api/orders", params={"venue": "fake", "order_id": "1"}).status_code == 200
        assert client.get("/api/orders").json()[0]["status"] == "cancelled"
        assert client.post("/api/builder/approve", json={"venue": "fake", "max_fee_bps": "1"}).status_code == 200
        assert client.get("/api/builder/status", params={"venue": "fake"}).json() == {"builder": BUILDER}
        assert client.get("/api/builder/earnings", params={"venue": "fake"}).status_code == 200
        assert client.post("/api/fills/sync").json() == {"fake": 1}
        assert len(client.get("/api/fills", params={"venue": "fake"}).json()) == 1


def test_api_maps_not_configured_and_upstream_errors(env, tmp_path):
    client, service, venue = make_client(env, tmp_path)

    async def missing():
        raise NotConfigured("POLY_PRIVATE_KEY is not set")

    async def broken(symbol):
        raise RuntimeError("upstream down")

    venue.account = missing
    venue.orderbook = broken
    with TestClient(client.app, raise_server_exceptions=False) as c:
        r = c.get("/api/account", params={"venue": "fake"})
        assert r.status_code == 409 and r.json()["kind"] == "not_configured"
        r = c.get("/api/book", params={"venue": "fake", "symbol": "X"})
        assert r.status_code == 502 and "upstream down" in r.json()["error"]


def test_real_venues_build_without_network(env, tmp_path):
    s = settings(env, HL_BUILDER_ADDRESS=BUILDER, HL_BUILDER_FEE_BPS="1", POLY_BUILDER_CODE=CODE)
    venues, _ = build_venues(s)
    assert sorted(venues) == ["hyperliquid", "polymarket", "polymarket_perps"]
    assert venues["hyperliquid"].builder_identity()["order_field"] == {"b": BUILDER, "f": 10}
    assert venues["polymarket"].builder_identity()["builder_code"] == CODE
