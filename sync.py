#!/usr/bin/env python3
import logging
import os
import time
from datetime import datetime, timezone

from activity import Activity
from coingecko import CoinResolver
from exchanges.base import Exchange
from exchanges.bitget import BitgetExchange
from ghostfolio import GhostfolioClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("sync")

GHOSTFOLIO_URL = os.environ["GHOSTFOLIO_URL"].rstrip("/")
GHOSTFOLIO_SECURITY_TOKEN = os.environ["GHOSTFOLIO_SECURITY_TOKEN"]
SYNC_INTERVAL_SECONDS = int(os.environ.get("SYNC_INTERVAL_SECONDS", "3600"))
DRIFT_TOLERANCE = float(os.environ.get("DRIFT_TOLERANCE", "0.005"))


def build_exchanges() -> list[Exchange]:
    exs: list[Exchange] = []
    if os.environ.get("BITGET_API_KEY"):
        exs.append(BitgetExchange(
            api_key=os.environ["BITGET_API_KEY"],
            api_secret=os.environ["BITGET_API_SECRET"],
            passphrase=os.environ["BITGET_API_PASSPHRASE"],
            account_name=os.environ.get("BITGET_ACCOUNT_NAME", "Bitget"),
        ))
    return exs


def _to_gf_dict(a: Activity, cg_id: str, unit_price: float, account_id: str) -> dict:
    return {
        "accountId": account_id,
        "currency": "USD",
        "dataSource": "COINGECKO",
        "date": datetime.fromtimestamp(a.ts_ms / 1000, tz=timezone.utc).isoformat(),
        "fee": round(a.fee_usd, 8),
        "quantity": a.quantity,
        "symbol": cg_id,
        "type": a.type,
        "unitPrice": unit_price,
        "comment": a.comment(),
    }


def _log_drift(name: str, ex_balances: dict[str, float], gf_holdings: dict[str, float],
               resolver: CoinResolver, tolerance: float) -> None:
    for symbol, ex_qty in sorted(ex_balances.items()):
        if ex_qty <= 0:
            continue
        cg_id = resolver.resolve(symbol)
        if not cg_id:
            log.info("[%s] %s: no CoinGecko id, skipping drift check (qty=%.8f)", name, symbol, ex_qty)
            continue
        gf_qty = gf_holdings.get(cg_id, 0.0)
        delta = abs(ex_qty - gf_qty)
        rel = delta / ex_qty
        if rel > tolerance:
            log.warning(
                "[%s] DRIFT on %s: exchange=%.8f vs ghostfolio=%.8f (delta=%.8f, %.2f%%)",
                name, symbol, ex_qty, gf_qty, delta, rel * 100,
            )
        else:
            log.info("[%s] %s reconciled: %.8f", name, symbol, ex_qty)


def sync_exchange(gf: GhostfolioClient, resolver: CoinResolver, ex: Exchange) -> None:
    account_id = gf.find_account(ex.account_name)
    log.info("[%s] account_id=%s", ex.name, account_id)
    existing = gf.list_existing_comments()
    log.info("[%s] %d existing activities cached for dedup", ex.name, len(existing))

    raw = ex.fetch_activities(ex.get_since_ms())
    log.info("[%s] fetched %d raw activities", ex.name, len(raw))

    gf_acts: list[dict] = []
    skipped_dup = skipped_resolve = 0
    for a in raw:
        if a.comment() in existing:
            skipped_dup += 1
            continue
        cg_id = resolver.resolve(a.symbol)
        if not cg_id:
            skipped_resolve += 1
            continue
        # No historical price lookup. For non-stablecoin deposits/withdrawals
        # (where Bitget doesn't give us a USD price), use $0 cost basis — the
        # quantity is still correct, which is what drift reconciliation needs.
        unit_price = a.unit_price_usd if a.unit_price_usd is not None else 0.0
        gf_acts.append(_to_gf_dict(a, cg_id, unit_price, account_id))

    log.info("[%s] dedup-skip=%d unresolved-symbol=%d submit=%d",
             ex.name, skipped_dup, skipped_resolve, len(gf_acts))

    if gf_acts:
        new, dup, err = gf.post_import(gf_acts)
        log.info("[%s] import: new=%d server_dups=%d errors=%d", ex.name, new, dup, err)
    else:
        log.info("[%s] nothing new to import", ex.name)

    try:
        ex_balances = ex.fetch_current_balances()
        gf_holdings = gf.fetch_account_holdings(account_id)
        _log_drift(ex.name, ex_balances, gf_holdings, resolver, DRIFT_TOLERANCE)
    except Exception:
        log.exception("[%s] drift check failed", ex.name)


def sync_cycle(resolver: CoinResolver, exchanges: list[Exchange]) -> None:
    gf = GhostfolioClient(GHOSTFOLIO_URL, GHOSTFOLIO_SECURITY_TOKEN)
    gf.login()
    log.info("authenticated with Ghostfolio")
    for ex in exchanges:
        try:
            sync_exchange(gf, resolver, ex)
        except Exception:
            log.exception("[%s] sync failed", ex.name)


def main() -> None:
    resolver = CoinResolver()
    exchanges = build_exchanges()
    if not exchanges:
        log.warning("no exchanges configured; idling")
    log.info("configured exchanges: %s", [e.name for e in exchanges])
    while True:
        try:
            sync_cycle(resolver, exchanges)
        except Exception:
            log.exception("sync cycle failed")
        log.info("sleeping %ds until next cycle", SYNC_INTERVAL_SECONDS)
        time.sleep(SYNC_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
