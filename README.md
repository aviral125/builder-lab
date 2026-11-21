# builder-lab

A small local backend for experimenting with builder codes on three venues:

| Venue key | What it is | Builder identity |
| --- | --- | --- |
| `polymarket` | Polymarket prediction markets (CLOB) | bytes32 builder code from your builder profile |
| `polymarket_perps` | Polymarket Perps | builder's Perps account address |
| `hyperliquid` | Hyperliquid perps | builder's wallet address |

One FastAPI process, one SQLite file, one web page (no build step). It runs on your
machine and binds to `127.0.0.1`.

## Run it

```bash
make install          # creates .venv and installs dependencies
cp .env.example .env  # then fill in what you have
make run              # http://127.0.0.1:8000
make test
```

It starts with nothing configured. Market data works straight away; anything
that needs a key answers `409 not_configured` and says which variable is missing.

## Safety rails

- `DRY_RUN=true` is the default. Orders, cancels and approvals are recorded
  locally and nothing is sent to a venue. Set `DRY_RUN=false` to go live.
- `MAX_ORDER_USD` caps price × size on every order. Market orders are priced
  off the venue's current price before the check.
- `HL_NETWORK=testnet` is the default for Hyperliquid.
- Keys are read from `.env` only. The API never returns them, and `.env` is
  git-ignored. The server has no authentication, so do not change `HOST`.

## What you need per venue

**Polymarket prediction markets**
1. Create a builder profile at polymarket.com/settings?tab=builder and copy the
   builder code into `POLY_BUILDER_CODE`. Fee rates are set on that page.
2. Put the trader's signer key in `POLY_PRIVATE_KEY`.
3. Every order then carries the code. No trader approval step exists here.

**Hyperliquid**
1. Builder wallet: any address you control with at least 100 USDC of perps
   account value. Put the address in `HL_BUILDER_ADDRESS`. Its key is not needed.
2. Trader wallet (a different one): key in `HL_PRIVATE_KEY`. It must be the main
   wallet, because the fee approval cannot be signed by an agent wallet.
3. Set `HL_BUILDER_FEE_BPS`, then approve once from the page (Builder → Approve).
4. Orders carry `{"b": builder, "f": fee}`. Fees are claimed on Hyperliquid
   through the referral reward claim.

**Polymarket Perps**
1. The builder's Perps account must be registered and enabled by Polymarket.
   "Builder status" shows `registered / enabled / admission_enabled`. Until all
   three are true, orders go out without attribution.
2. Put the address in `POLY_PERPS_BUILDER_ADDRESS` and the max fee in
   `POLY_PERPS_BUILDER_FEE_BPS`. The trader is `POLY_PRIVATE_KEY` and must be a
   different account.
3. Approve once from the page. Builder earnings can only be read by the builder
   account, so that view needs `POLY_PERPS_BUILDER_PRIVATE_KEY`.

## API

All routes take `venue` = `polymarket` | `polymarket_perps` | `hyperliquid`.

| Route | Purpose |
| --- | --- |
| `GET /api/config` | Mode, limits, builder identities (no keys) |
| `GET /api/markets?venue=&q=` | List or search markets |
| `GET /api/book?venue=&symbol=` | Order book |
| `GET /api/account?venue=` | Balances, positions, open orders |
| `POST /api/orders` | Place an order: `{venue, symbol, side, size, price?, tif?, post_only?, reduce_only?}` |
| `DELETE /api/orders?venue=&order_id=&symbol=` | Cancel |
| `GET /api/orders`, `GET /api/fills` | Local records |
| `POST /api/fills/sync` | Pull fills now (also runs every `POLL_SECONDS`) |
| `GET /api/builder/status?venue=` | Builder eligibility and the trader's approval |
| `POST /api/builder/approve` | Trader approves `{venue, max_fee_bps}`; 0 revokes |
| `GET /api/builder/earnings?venue=` | Builder earnings |

`symbol` is a token id on `polymarket`, an instrument id on `polymarket_perps`,
and a coin such as `BTC` on `hyperliquid`. Fees are always in basis points here;
each adapter converts to the venue's own unit.

## Layout

```
app/config.py              env loading and validation
app/signer.py              Signer abstraction (EnvKeySigner today)
app/db.py                  SQLite: orders, fills, approvals, builder_earnings
app/service.py             validation, safety rails, persistence, fill poller
app/main.py                routes
app/venues/base.py         shared Venue interface
app/venues/polymarket_clob.py
app/venues/polymarket_perps.py
app/venues/hyperliquid.py
app/static/index.html      the page
tests/                     46 tests against fake SDKs, no network
```

## Known limits

- Not tested against live venues yet. The tests use fakes of both SDKs, checked
  against `polymarket-client` 0.20.1 and `hyperliquid-python-sdk` 0.24.0. Start
  on Hyperliquid testnet, then one minimum-size order per venue.
- The Polymarket SDK marks its perps API as experimental; versions are pinned.
- No deposits, withdrawals, websockets, or multi-user auth.
- Toward a real app: tables are already keyed by trader address, and adapters
  take a `Signer`. The next step is client-side signing so the server holds no
  trader keys.
- Polymarket's builder terms exclude self-referred and non-bona fide volume.
  Check with them before routing your own trades through your own fee-charging code.
