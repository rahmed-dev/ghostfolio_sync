import json
import logging
import time

import requests

log = logging.getLogger("ghostfolio")


class GhostfolioClient:
    def __init__(self, base_url: str, security_token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.security_token = security_token
        self._jwt: str | None = None

    def login(self) -> None:
        r = requests.post(
            f"{self.base_url}/api/v1/auth/anonymous",
            json={"accessToken": self.security_token},
            timeout=30,
        )
        r.raise_for_status()
        self._jwt = r.json()["authToken"]

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._jwt}"}

    def find_account(self, name: str) -> str:
        r = requests.get(f"{self.base_url}/api/v1/account", headers=self._headers(), timeout=30)
        r.raise_for_status()
        for a in r.json().get("accounts", []):
            if a.get("name") == name:
                return a["id"]
        raise RuntimeError(
            f"Account '{name}' not found in Ghostfolio. "
            f"Create it in the UI: Accounts → Add Account → name='{name}', currency=USD."
        )

    def _fetch_activities(self, account_id: str | None = None) -> list[dict]:
        params = {"accounts": account_id} if account_id else None
        r = requests.get(
            f"{self.base_url}/api/v1/activities",
            headers=self._headers(),
            params=params,
            timeout=60,
        )
        r.raise_for_status()
        return r.json().get("activities", [])

    def list_existing_comments(self) -> set[str]:
        return {a["comment"] for a in self._fetch_activities() if a.get("comment")}

    def fetch_account_holdings(self, account_id: str) -> dict[str, float]:
        """Compute per-symbol qty from activities of one account.
        Returns {coingecko_id: qty}."""
        holdings: dict[str, float] = {}
        for a in self._fetch_activities(account_id):
            if a.get("accountId") != account_id:
                continue
            sym = (a.get("SymbolProfile") or {}).get("symbol") or a.get("symbol")
            if not sym:
                continue
            qty = float(a.get("quantity") or 0)
            t = a.get("type")
            if t in ("BUY", "INTEREST"):
                holdings[sym] = holdings.get(sym, 0.0) + qty
            elif t == "SELL":
                holdings[sym] = holdings.get(sym, 0.0) - qty
        return holdings

    def _submit(self, batch: list[dict]) -> requests.Response:
        return requests.post(
            f"{self.base_url}/api/v1/import",
            headers={**self._headers(), "Content-Type": "application/json"},
            json={"activities": batch},
            timeout=120,
        )

    @staticmethod
    def _err_text(r: requests.Response) -> str:
        try:
            body = r.json()
        except Exception:
            return r.text
        msg = body.get("message", body)
        return msg if isinstance(msg, str) else json.dumps(msg)

    def _submit_with_retry(self, batch: list[dict], attempts: int = 3) -> list[dict] | None:
        last_err = ""
        for i in range(attempts):
            r = self._submit(batch)
            if r.ok:
                try:
                    return r.json().get("activities", []) or []
                except Exception:
                    return []
            text = self._err_text(r)
            last_err = text
            if "is not valid for the specified data source" in text.lower():
                wait = 15 * (i + 1)
                log.warning(
                    "data source validation failed (likely CoinGecko rate limit); "
                    "sleeping %ds before retry %d/%d", wait, i + 2, attempts,
                )
                time.sleep(wait)
                continue
            log.error("import batch failed: %s", text)
            return None
        log.error("import batch failed after %d attempts: %s", attempts, last_err)
        return None

    def post_import(self, activities: list[dict], batch_size: int = 5,
                    inter_batch_sleep_sec: float = 8.0) -> tuple[int, int, int]:
        """Submit activities grouped by symbol so GF only re-validates each symbol
        once per batch. Bigger inter-batch sleep keeps the CoinGecko free quota happy."""
        by_symbol: dict[str, list[dict]] = {}
        for a in activities:
            by_symbol.setdefault(a["symbol"], []).append(a)
        new, dup, err = 0, 0, 0
        symbols = sorted(by_symbol.keys())
        for sym in symbols:
            group = by_symbol[sym]
            for i in range(0, len(group), batch_size):
                batch = group[i : i + batch_size]
                result = self._submit_with_retry(batch)
                if result is None:
                    err += len(batch)
                else:
                    new += len(result)
                    dup += len(batch) - len(result)
                time.sleep(inter_batch_sleep_sec)
        return new, dup, err
