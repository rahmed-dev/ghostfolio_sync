import base64
import hashlib
import hmac
import json
import logging
import time

import requests

from activity import Activity, PriceTracker
from coingecko import STABLECOINS
from exchanges.base import Exchange

log = logging.getLogger("bitget")

BITGET_BASE = "https://api.bitget.com"
QUOTE_CURRENCY = "USDT"


class BitgetExchange(Exchange):
    name = "Bitget"

    def __init__(self, api_key: str, api_secret: str, passphrase: str, account_name: str) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.account_name = account_name

    def _sign(self, ts: str, method: str, path: str, body: str = "") -> str:
        msg = f"{ts}{method}{path}{body}"
        digest = hmac.new(self.api_secret.encode(), msg.encode(), hashlib.sha256).digest()
        return base64.b64encode(digest).decode()

    def _get(self, path: str, params: dict | None = None) -> dict:
        query = ""
        if params:
            pairs = [f"{k}={v}" for k, v in sorted(params.items()) if v is not None]
            if pairs:
                query = "?" + "&".join(pairs)
        request_path = path + query
        ts = str(int(time.time() * 1000))
        headers = {
            "ACCESS-KEY": self.api_key,
            "ACCESS-SIGN": self._sign(ts, "GET", request_path),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "locale": "en-US",
        }
        r = requests.get(BITGET_BASE + request_path, headers=headers, timeout=30)
        if not r.ok:
            raise RuntimeError(f"Bitget {r.status_code} on {request_path}: {r.text[:500]}")
        data = r.json()
        if data.get("code") != "00000":
            raise RuntimeError(f"Bitget API error {data.get('code')}: {data.get('msg')} on {path}")
        return data

    BITGET_API_CAP_MS = 90 * 24 * 3600 * 1000

    def _windowed_start(self, since_ms: int) -> int:
        floor_ms = int(time.time() * 1000) - self.BITGET_API_CAP_MS
        if since_ms < floor_ms:
            log.warning("requested window pre-dates Bitget's 90d API cap; truncating")
            return floor_ms
        return since_ms

    def _extract_fee_usd(self, fd_raw, base: str, price: float, trade_id, prices: PriceTracker) -> float:
        items: list[dict] = []
        if isinstance(fd_raw, str) and fd_raw.strip():
            try:
                fd_raw = json.loads(fd_raw)
            except Exception:
                return 0.0
        if isinstance(fd_raw, dict):
            items = [v for v in fd_raw.values() if isinstance(v, dict)]
        elif isinstance(fd_raw, list):
            items = [v for v in fd_raw if isinstance(v, dict)]
        fee_usd = 0.0
        for fd in items:
            fee_coin = (fd.get("feeCoin") or fd.get("feeCoinCode") or "").upper()
            fee_amt = abs(float(fd.get("totalFee") or 0))
            if fee_amt == 0:
                continue
            if fee_coin in STABLECOINS:
                fee_usd += fee_amt
            elif fee_coin == base:
                fee_usd += fee_amt * price
            else:
                tracked = prices.get(fee_coin)
                if tracked:
                    fee_usd += fee_amt * tracked
                else:
                    log.info("fee in %s no tracked price yet (tradeId=%s); fee_usd=0 for this leg",
                             fee_coin, trade_id)
        return fee_usd

    # ---------- spot ----------

    def _fetch_spot_fills(self, since_ms: int) -> list[dict]:
        fills, cursor = [], None
        now_ms = int(time.time() * 1000)
        while True:
            params: dict = {"limit": "100", "startTime": since_ms, "endTime": now_ms}
            if cursor:
                params["idLessThan"] = cursor
            resp = self._get("/api/v2/spot/trade/fills", params)
            page = resp.get("data") or []
            if not page:
                break
            fills.extend(page)
            if len(page) < 100:
                break
            cursor = page[-1].get("tradeId")
            if not cursor:
                break
            time.sleep(0.2)
        return fills

    def _spot_activities(self, since_ms: int) -> list[Activity]:
        raw = self._fetch_spot_fills(since_ms)
        # Oldest first so each fill's base price feeds the tracker for later fees
        raw.sort(key=lambda f: int(f.get("cTime") or 0))
        prices = PriceTracker()
        out: list[Activity] = []
        for f in raw:
            symbol = f.get("symbol", "")
            if not symbol.endswith(QUOTE_CURRENCY):
                log.warning("skipping non-%s spot fill: %s id=%s",
                            QUOTE_CURRENCY, symbol, f.get("tradeId"))
                continue
            base = symbol[: -len(QUOTE_CURRENCY)]
            side = (f.get("side") or "").upper()
            if side not in ("BUY", "SELL"):
                continue
            price = float(f.get("priceAvg") or f.get("price") or 0)
            qty = float(f.get("size") or 0)
            ts_ms = int(f.get("cTime") or 0)
            if price <= 0 or qty <= 0 or ts_ms <= 0:
                continue
            prices.update(base, price)
            trade_id = f.get("tradeId")
            fee_usd = self._extract_fee_usd(f.get("feeDetail"), base, price, trade_id, prices)
            quote_qty = qty * price

            # base coin leg (matches v1 comment format for backward dedup)
            out.append(Activity(
                ts_ms=ts_ms, type=side, symbol=base,
                quantity=qty, unit_price_usd=price, fee_usd=fee_usd,
                exchange=self.name, source="spot", external_id=str(trade_id),
            ))
            # paired quote coin leg (USDT) so balance reconciles
            opposite = "SELL" if side == "BUY" else "BUY"
            out.append(Activity(
                ts_ms=ts_ms, type=opposite, symbol=QUOTE_CURRENCY,
                quantity=quote_qty, unit_price_usd=1.0, fee_usd=0.0,
                exchange=self.name, source="spot-quote", external_id=str(trade_id),
            ))
        return out

    # ---------- earn ----------

    def _fetch_earn(self, since_ms: int) -> list[dict]:
        now_ms = int(time.time() * 1000)
        try:
            resp = self._get(
                "/api/v2/earn/savings/records",
                {"startTime": since_ms, "endTime": now_ms, "limit": "100"},
            )
        except Exception as e:
            log.warning("earn fetch failed (ok if you don't use Bitget Earn): %s", e)
            return []
        data = resp.get("data")
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("resultList") or data.get("records") or data.get("list") or []
        else:
            items = []
        return [r for r in items if isinstance(r, dict)]

    def _earn_activities(self, since_ms: int) -> list[Activity]:
        out: list[Activity] = []
        for r in self._fetch_earn(since_ms):
            coin = (r.get("coin") or "").upper()
            amount = float(r.get("amount") or 0)
            ts_ms = int(r.get("cTime") or r.get("timestamp") or 0)
            if not coin or amount <= 0 or ts_ms <= 0:
                continue
            rec_id = r.get("recordId") or r.get("id") or f"{coin}-{ts_ms}"
            price = 1.0 if coin in STABLECOINS else None
            out.append(Activity(
                ts_ms=ts_ms, type="INTEREST", symbol=coin,
                quantity=amount, unit_price_usd=price, fee_usd=0.0,
                exchange=self.name, source="earn", external_id=str(rec_id),
            ))
        return out

    # ---------- deposits / withdrawals ----------

    def _fetch_wallet_records(self, endpoint: str, since_ms: int) -> list[dict]:
        records, cursor = [], None
        now_ms = int(time.time() * 1000)
        while True:
            params: dict = {"limit": "100", "startTime": since_ms, "endTime": now_ms}
            if cursor:
                params["idLessThan"] = cursor
            try:
                resp = self._get(endpoint, params)
            except Exception as e:
                log.warning("%s fetch failed: %s", endpoint, e)
                return records
            page = resp.get("data") or []
            if not page:
                break
            records.extend(page)
            if len(page) < 100:
                break
            cursor = page[-1].get("orderId") or page[-1].get("id")
            if not cursor:
                break
            time.sleep(0.2)
        return records

    def _wallet_activities(self, endpoint: str, source: str, side: str, since_ms: int) -> list[Activity]:
        out: list[Activity] = []
        for r in self._fetch_wallet_records(endpoint, since_ms):
            status = (r.get("status") or "").lower()
            # Only count completed transfers; pending/cancelled don't affect balance
            if status and status not in ("success", "successful", "completed", "complete"):
                continue
            coin = (r.get("coin") or "").upper()
            amount = float(r.get("size") or r.get("amount") or 0)
            ts_ms = int(r.get("cTime") or r.get("uTime") or 0)
            if not coin or amount <= 0 or ts_ms <= 0:
                continue
            rec_id = r.get("orderId") or r.get("tradeId") or r.get("id") or f"{coin}-{ts_ms}"
            price = 1.0 if coin in STABLECOINS else None
            fee = float(r.get("fee") or 0)
            fee_usd = fee if coin in STABLECOINS else 0.0  # crypto-fees ignored
            out.append(Activity(
                ts_ms=ts_ms, type=side, symbol=coin,
                quantity=amount, unit_price_usd=price, fee_usd=fee_usd,
                exchange=self.name, source=source, external_id=str(rec_id),
            ))
        return out

    # ---------- public ----------

    def fetch_activities(self, since_ms: int) -> list[Activity]:
        start = self._windowed_start(since_ms)
        acts: list[Activity] = []
        acts += self._spot_activities(start)
        log.info("[Bitget] spot legs: %d", len(acts))
        n_before = len(acts)
        acts += self._earn_activities(start)
        log.info("[Bitget] earn records: %d", len(acts) - n_before)
        n_before = len(acts)
        acts += self._wallet_activities(
            "/api/v2/spot/wallet/deposit-records", source="deposit", side="BUY", since_ms=start,
        )
        log.info("[Bitget] deposits: %d", len(acts) - n_before)
        n_before = len(acts)
        acts += self._wallet_activities(
            "/api/v2/spot/wallet/withdrawal-records", source="withdrawal", side="SELL", since_ms=start,
        )
        log.info("[Bitget] withdrawals: %d", len(acts) - n_before)
        return acts

    def fetch_current_balances(self) -> dict[str, float]:
        resp = self._get("/api/v2/spot/account/assets")
        balances: dict[str, float] = {}
        for row in resp.get("data") or []:
            coin = (row.get("coin") or "").upper()
            if not coin:
                continue
            available = float(row.get("available") or 0)
            frozen = float(row.get("frozen") or 0)
            locked = float(row.get("locked") or 0)
            qty = available + frozen + locked
            if qty > 0:
                balances[coin] = qty
        return balances
