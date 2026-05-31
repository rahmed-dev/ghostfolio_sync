# ghostfolio_sync (multi-exchange → Ghostfolio)

A small Python service that pulls **read-only trade + earn + deposit + withdrawal history** from supported exchanges and pushes them into a self-hosted [Ghostfolio](https://github.com/ghostfolio/ghostfolio) instance via the `/api/v1/import` API. Runs as one Docker container on an hourly loop, syncing every configured exchange in turn.

Built because Ghostfolio has no native exchange connectors.

**Supported exchanges:** Bitget. The architecture is designed for easy extension — see "Adding a new exchange" below.

---

## What it syncs (per exchange)

| Source | Ghostfolio activity |
| --- | --- |
| Spot fills | `BUY` / `SELL` of base coin **plus** paired `SELL` / `BUY` of `USDT @ $1` (so USDT balance reconciles) |
| Earn / savings rewards | `INTEREST` |
| Wallet deposits | `BUY` (price = $1 for stables, CoinGecko historical for crypto) |
| Wallet withdrawals | `SELL` (same price logic) |

Non-USDT spot pairs are skipped (the paired-activity model assumes USDT quote). Futures/perps are out of scope by design.

## How it stays reconciled

After every sync cycle, the service compares **Bitget's reported balance** (`/spot/account/assets`) against **Ghostfolio's computed holding** (sum of `BUY` + `INTEREST` − `SELL` per symbol) and logs:

- `[Bitget] BTC reconciled: 0.12345678` — within 0.5% tolerance
- `[Bitget] DRIFT on BTC: exchange=0.12345678 vs ghostfolio=0.10000000 (delta=0.02, 18.93%)` — means a flow is missing

Drift is informational, not a hard error. Tolerance is configurable via `DRIFT_TOLERANCE` env var (default `0.005` = 0.5%).

## How dedup works

Each activity is posted with a stable `comment` (e.g. `Bitget:spot:<tradeId>`, `Bitget:deposit:<orderId>`). On every cycle the service queries existing comments and filters them out client-side before submitting — so re-fetching is idempotent and safe.

## Window sizing

Every cycle fetches the last **24h** of activity (configurable via `LOOKBACK_HOURS`). No special bootstrap window — this keeps Ghostfolio's CoinGecko validation under free-tier rate limits.

**One-time historical backfill** (if you want trades older than 24h imported):

1. Stop container: `docker compose stop bitget-sync`
2. Set `LOOKBACK_HOURS=2160` in `.env.sync` (= 90 days, Bitget's API cap)
3. Start: `docker compose up -d bitget-sync` — first cycle will grind through hundreds of activities slowly
4. Once it finishes, set back to `LOOKBACK_HOURS=24` and restart.

Trades older than 90 days require a CSV export from Bitget UI + manual import via Ghostfolio UI.

---

## File layout (this repo)

```
.
├── Dockerfile
├── requirements.txt              # just `requests`
├── sync.py                       # orchestrator: loop, per-exchange driver, drift
├── ghostfolio.py                 # GhostfolioClient (login, account, activities, import)
├── coingecko.py                  # hardcoded ticker → CoinGecko-ID map (NO HTTP calls)
├── activity.py                   # Activity dataclass (exchange-agnostic intermediate)
├── exchanges/
│   ├── base.py                   # Exchange ABC
│   └── bitget.py                 # BitgetExchange
├── .gitignore
└── README.md
```

## File layout (host machine, NOT in this repo)

```
/home/riz/ghostfolio/
├── .env                          # Ghostfolio's postgres/redis secrets
├── .env.sync                     # THIS service's secrets (exchange keys + GF token)
├── docker/docker-compose.yml     # has a `bitget-sync` service entry
└── sync/                         # clone of THIS repo
```

`.env.sync` lives one directory up **on purpose** — it never enters this repo. `.gitignore` defensively excludes `.env*` anyway.

---

## Setup on a fresh machine

### 1. Pre-requirements

- Ghostfolio already running.
- Docker + docker compose.
- Bitget **read-only** API key (key + secret + passphrase) — Bitget UI → API Management → permission `Read` only.
- Ghostfolio security token — Ghostfolio UI → My Ghostfolio → Security Token.

### 2. Create the "Bitget" account in Ghostfolio UI (one-time)

- Accounts → **+ Add Account**
- Name: `Bitget` (case-sensitive, must match `BITGET_ACCOUNT_NAME`)
- Currency: `USD`

### 3. Clone this repo

```bash
git clone https://github.com/rahmed-dev/ghostfolio_sync.git /home/riz/ghostfolio/sync
```

### 4. Create `.env.sync` (one level above the repo)

```bash
cat > /home/riz/ghostfolio/.env.sync <<'EOF'
BITGET_API_KEY=
BITGET_API_SECRET=
BITGET_API_PASSPHRASE=
GHOSTFOLIO_URL=http://ghostfolio:3333
GHOSTFOLIO_SECURITY_TOKEN=
BITGET_ACCOUNT_NAME=Bitget
SYNC_INTERVAL_SECONDS=3600
DRIFT_TOLERANCE=0.005
EOF
chmod 600 /home/riz/ghostfolio/.env.sync
```

### 5. Add the service to your Ghostfolio compose file

Append under `services:` in `/home/riz/ghostfolio/docker/docker-compose.yml`:

```yaml
  bitget-sync:
    build: ../sync
    container_name: bitget-sync
    restart: unless-stopped
    env_file:
      - ../.env.sync
    depends_on:
      ghostfolio:
        condition: service_healthy
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
```

### 6. Build and start

```bash
cd /home/riz/ghostfolio/docker
docker compose up -d --build bitget-sync
docker logs -f bitget-sync
```

---

## Environment variables

| Var | Purpose | Default |
| --- | --- | --- |
| `BITGET_API_KEY` / `BITGET_API_SECRET` / `BITGET_API_PASSPHRASE` | Bitget read-only credentials | required |
| `GHOSTFOLIO_URL` | GF API base URL (internal hostname recommended) | required |
| `GHOSTFOLIO_SECURITY_TOKEN` | From GF UI → Security Token | required |
| `BITGET_ACCOUNT_NAME` | GF account to attribute Bitget activities to | `Bitget` |
| `SYNC_INTERVAL_SECONDS` | Time between cycles | `3600` |
| `LOOKBACK_HOURS` | How far back each cycle fetches | `24` |
| `DRIFT_TOLERANCE` | Drift-warning threshold (fraction) | `0.005` |

---

## Adding a new coin

The sync uses a **hardcoded ticker → CoinGecko-ID map** in `coingecko.py` — no runtime HTTP calls, no CoinGecko rate-limit pressure. The map has two sources:

- `TOP_COINS` — auto-generated by `scripts/refresh_coin_map.py` (top 100 by market cap)
- `EXTRA_COINS` — manual overrides for tickers outside the top 100 (e.g. `PROVE`)

When you trade an unknown coin, the log says:

```
WARNING coingecko no CoinGecko id mapped for XYZ — add to EXTRA_COINS in coingecko.py or run scripts/refresh_coin_map.py for a fresh top-N pull
```

**Two fix options:**

1. **Refresh top-N** (if XYZ has grown into the top 100):
   ```bash
   python3 scripts/refresh_coin_map.py 100 > /tmp/new_top.py
   # copy the COIN_IDS body into TOP_COINS in coingecko.py
   ```

2. **Manual entry** (for niche coins):
   - Look up the slug at coingecko.com (the URL part: `coingecko.com/en/coins/<slug>`)
   - Add to `EXTRA_COINS` in `coingecko.py`:
     ```python
     EXTRA_COINS = {
         "XYZ": "xyz-coingecko-slug",
     }
     ```

Then rebuild:
```bash
cd /home/riz/ghostfolio/docker
docker compose up -d --build bitget-sync
```

---

## Adding a new exchange (e.g. Bybit)

1. **Create `exchanges/bybit.py`** implementing the `Exchange` ABC:

```python
from activity import Activity
from exchanges.base import Exchange

class BybitExchange(Exchange):
    name = "Bybit"

    def __init__(self, api_key, api_secret, account_name):
        super().__init__()
        self.api_key = api_key
        self.api_secret = api_secret
        self.account_name = account_name

    def fetch_activities(self, since_ms: int) -> list[Activity]:
        # Pull spot fills, earn, deposits, withdrawals.
        # For each spot trade, emit a paired Activity for the quote currency
        # so the balance reconciles (see exchanges/bitget.py for reference).
        ...

    def fetch_current_balances(self) -> dict[str, float]:
        # symbol (uppercase) → quantity. Used for the drift check.
        ...
```

2. **Register in `sync.py`** by adding to `build_exchanges()`:

```python
if os.environ.get("BYBIT_API_KEY"):
    from exchanges.bybit import BybitExchange
    exs.append(BybitExchange(
        api_key=os.environ["BYBIT_API_KEY"],
        api_secret=os.environ["BYBIT_API_SECRET"],
        account_name=os.environ.get("BYBIT_ACCOUNT_NAME", "Bybit"),
    ))
```

3. **Add credentials to `.env.sync`** and **create a "Bybit" account in the Ghostfolio UI** (same as Bitget step 2).

4. **Rebuild**: `docker compose up -d --build bitget-sync`.

The orchestrator runs each exchange in turn; one failing exchange doesn't block the others.

---

## Operational notes

- **Restart policy:** `unless-stopped`. Auto-starts at boot once Docker is up.
- **Persistence:** None needed — the script is stateless. The only thing to back up is `.env.sync` (alongside Ghostfolio's `.env`).
- **Logs:** `docker logs bitget-sync`. Each cycle prints per-exchange counts, dedup stats, import results, and drift reconciliation lines.

## Limitations / things explicitly NOT done

- **Futures/perpetuals** not synced — would need different endpoints + GF doesn't model perps well.
- **Non-USDT spot pairs** (BTC/ETH etc.) skipped — paired-activity model assumes USDT quote.
- **Convert/swap history** not synced — Bitget exposes `/api/v2/convert/convert-record`, easy to add when needed.
- **Backfill >90 days** — Bitget's API cap; older trades require a UI CSV export.
- **External alerts** — drift is logged, not paged. Wire up Healthchecks.io / similar yourself if you want notifications.

## Tech choices (rationale)

- **Direct HTTP, not `ccxt`** — earn/deposit/withdraw endpoints need direct HTTP anyway. Keeps the image at ~80MB with one dep.
- **Stateless** — re-fetch + client-side dedup is simpler than maintaining a last-sync cursor on disk. Restart cost is one extra 90d cycle.
- **Sleep loop in entrypoint** — self-contained, no host cron.
- **One container, many exchanges** — single process, single GF login per cycle, cheaper than one container per exchange.
