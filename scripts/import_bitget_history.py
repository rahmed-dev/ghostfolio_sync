#!/usr/bin/env python3
"""One-shot importer for Bitget account-ledger TSV export.

Parses scripts/bitget_history.tsv, reconstructs trade groups + transfers + interest +
bot moves, and posts them into Ghostfolio via the same client the live sync uses.

Run from inside the bitget-sync container (so imports + GF network access work):

    docker cp scripts/import_bitget_history.py bitget-sync:/app/scripts/
    docker cp scripts/bitget_history.tsv      bitget-sync:/app/scripts/
    docker exec -e PYTHONPATH=/app \\
        -e GHOSTFOLIO_URL=$(docker exec bitget-sync printenv GHOSTFOLIO_URL) \\
        -e GHOSTFOLIO_SECURITY_TOKEN=$(...) \\
        bitget-sync python3 /app/scripts/import_bitget_history.py
"""
import csv
import logging
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/app")

from activity import Activity, PriceTracker
from coingecko import CoinResolver
from ghostfolio import GhostfolioClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("csv-import")

# Live sync owns the last 24h. Skip anything on or after this UTC time to avoid
# colliding with comments the live sync wrote with a different format.
CUTOFF = datetime(2026, 5, 30, 0, 0, 0, tzinfo=timezone.utc)
EXCHANGE = "Bitget"
TSV_PATH = os.environ.get("TSV_PATH", "/app/bitget_history.tsv")


def parse_ts(s: str) -> datetime:
    return datetime.strptime(s.strip(), "%d/%m/%Y %H:%M").replace(tzinfo=timezone.utc)


def load_rows(path: str) -> list[dict]:
    rows: list[dict] = []
    with open(path) as f:
        reader = csv.DictReader(f, delimiter="\t")
        for raw in reader:
            r: dict = {}
            r["order_id"] = raw["order"].strip().strip('"').strip()
            r["ts"] = parse_ts(raw["Date"])
            r["ts_ms"] = int(r["ts"].timestamp() * 1000)
            r["coin"] = raw["Coin"].strip().upper()
            r["type"] = raw["Type"].strip()
            r["amount"] = float(raw["Amount"] or 0)
            r["fee"] = float(raw["Fee"] or 0)
            rows.append(r)
    return rows


def build_activities(rows: list[dict]) -> list[Activity]:
    """Walk rows oldest-first; group spot trades as Coin->fee?->USDT triples."""
    rows = list(reversed(rows))  # TSV is newest-first; reverse for chronological
    prices = PriceTracker()
    out: list[Activity] = []
    buf: dict | None = None  # in-progress trade buffer: {"coin": row, "fee": row?}

    def close_buf(usdt_row: dict) -> None:
        nonlocal buf
        if buf is None:
            log.warning("USDT close row with no buffered coin leg (order %s); skipping",
                        usdt_row["order_id"])
            return
        coin_row = buf["coin"]
        fee_row = buf.get("fee")
        side = "BUY" if coin_row["type"].upper() == "BUY" else "SELL"
        coin = coin_row["coin"]
        gross_qty = abs(coin_row["amount"])
        self_fee_qty = abs(coin_row["fee"])  # fee paid in the coin itself
        net_qty = gross_qty - self_fee_qty
        usdt_qty = abs(usdt_row["amount"])
        if net_qty <= 0:
            log.warning("zero net qty for order %s; skipping", coin_row["order_id"])
            buf = None
            return
        unit_price = usdt_qty / net_qty
        prices.update(coin, unit_price)
        fee_usd = 0.0
        if fee_row is not None:
            fc = fee_row["coin"]
            fq = abs(fee_row["fee"])
            tracked = prices.get(fc)
            if tracked:
                fee_usd = fq * tracked
            else:
                log.info("no tracked price for fee coin %s yet (order %s); fee_usd=0",
                         fc, coin_row["order_id"])
        # base leg (qty net of any self-fee; unit_price = usdt/net)
        out.append(Activity(
            ts_ms=coin_row["ts_ms"], type=side, symbol=coin,
            quantity=net_qty, unit_price_usd=unit_price, fee_usd=fee_usd,
            exchange=EXCHANGE, source="csv-spot", external_id=coin_row["order_id"],
        ))
        # paired USDT leg @ $1 so USDT balance reconciles
        out.append(Activity(
            ts_ms=coin_row["ts_ms"], type="SELL" if side == "BUY" else "BUY",
            symbol="USDT", quantity=usdt_qty, unit_price_usd=1.0, fee_usd=0.0,
            exchange=EXCHANGE, source="csv-spot-quote", external_id=coin_row["order_id"],
        ))
        # separate fee-coin leg @ $0 so its qty reconciles without creating fake cash
        if fee_row is not None:
            fc = fee_row["coin"]
            fq = abs(fee_row["fee"])
            out.append(Activity(
                ts_ms=fee_row["ts_ms"], type="SELL", symbol=fc,
                quantity=fq, unit_price_usd=0.0, fee_usd=0.0,
                exchange=EXCHANGE, source="csv-fee", external_id=fee_row["order_id"],
            ))
        buf = None

    for r in rows:
        coin = r["coin"]
        t = r["type"]

        # ---- spot trade state machine ----
        if coin == "USDT" and t.lower() == "sell" and r["amount"] < 0:
            close_buf(r)
            continue
        if t == "Transaction fee deduct":
            if buf is None:
                log.warning("orphan fee row order %s; skipping", r["order_id"])
            else:
                buf["fee"] = r
            continue
        if t in ("Buy", "Sell"):
            if buf is not None:
                log.warning("unclosed buffer at order %s; dropping previous", r["order_id"])
            buf = {"coin": r}
            continue

        # ---- standalone events ----
        if t == "Interest":
            # BGB earn reward. Use BUY @ $0 (qty added, no cash impact)
            out.append(Activity(
                ts_ms=r["ts_ms"], type="BUY", symbol=coin,
                quantity=abs(r["amount"]), unit_price_usd=0.0, fee_usd=0.0,
                exchange=EXCHANGE, source="csv-interest", external_id=r["order_id"],
            ))
        elif t == "Transfer in":
            out.append(Activity(
                ts_ms=r["ts_ms"], type="BUY", symbol=coin,
                quantity=abs(r["amount"]),
                unit_price_usd=1.0 if coin == "USDT" else (prices.get(coin) or 0.0),
                fee_usd=0.0,
                exchange=EXCHANGE, source="csv-deposit", external_id=r["order_id"],
            ))
        elif t == "Transfer out":
            out.append(Activity(
                ts_ms=r["ts_ms"], type="SELL", symbol=coin,
                quantity=abs(r["amount"]),
                unit_price_usd=1.0 if coin == "USDT" else (prices.get(coin) or 0.0),
                fee_usd=0.0,
                exchange=EXCHANGE, source="csv-withdrawal", external_id=r["order_id"],
            ))
        elif t == "Financial":
            # Internal spot<->earn move; ownership unchanged
            continue
        elif t == "Opening of trading bot position":
            out.append(Activity(
                ts_ms=r["ts_ms"], type="SELL", symbol=coin,
                quantity=abs(r["amount"]),
                unit_price_usd=1.0 if coin == "USDT" else (prices.get(coin) or 0.0),
                fee_usd=0.0,
                exchange=EXCHANGE, source="csv-bot-open", external_id=r["order_id"],
            ))
        elif t == "Closing of trading bot position":
            if r["amount"] > 0:
                out.append(Activity(
                    ts_ms=r["ts_ms"], type="BUY", symbol=coin,
                    quantity=abs(r["amount"]),
                    unit_price_usd=1.0 if coin == "USDT" else (prices.get(coin) or 0.0),
                    fee_usd=0.0,
                    exchange=EXCHANGE, source="csv-bot-close", external_id=r["order_id"],
                ))
            # negative amount on close shouldn't normally happen; skip
        else:
            log.warning("unknown row type %r coin=%s order=%s; skipping",
                        t, coin, r["order_id"])

    return out


def main() -> None:
    rows = load_rows(TSV_PATH)
    log.info("loaded %d TSV rows", len(rows))

    acts = build_activities(rows)
    log.info("built %d activities (pre-cutoff)", len(acts))

    cutoff_ms = int(CUTOFF.timestamp() * 1000)
    acts = [a for a in acts if a.ts_ms < cutoff_ms]
    log.info("%d activities after cutoff (< %s)", len(acts), CUTOFF.isoformat())

    resolver = CoinResolver()
    gf = GhostfolioClient(
        os.environ["GHOSTFOLIO_URL"], os.environ["GHOSTFOLIO_SECURITY_TOKEN"],
    )
    gf.login()
    account_id = gf.find_account(os.environ.get("BITGET_ACCOUNT_NAME", "Bitget"))
    existing = gf.list_existing_comments()
    log.info("account_id=%s existing_comments=%d", account_id, len(existing))

    gf_acts: list[dict] = []
    dup = unresolved = 0
    for a in acts:
        if a.comment() in existing:
            dup += 1
            continue
        cg_id = resolver.resolve(a.symbol)
        if not cg_id:
            unresolved += 1
            continue
        unit_price = a.unit_price_usd if a.unit_price_usd is not None else 0.0
        gf_acts.append({
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
        })

    log.info("dedup-skip=%d unresolved=%d submit=%d", dup, unresolved, len(gf_acts))

    if not gf_acts:
        log.info("nothing to import")
        return

    new, server_dup, err = gf.post_import(gf_acts)
    log.info("import: new=%d server_dup=%d err=%d", new, server_dup, err)


if __name__ == "__main__":
    main()
