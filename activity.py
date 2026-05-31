from dataclasses import dataclass


@dataclass
class Activity:
    ts_ms: int
    type: str  # "BUY" | "SELL" | "INTEREST"
    symbol: str  # raw exchange ticker (e.g. "BTC", "USDT")
    quantity: float
    unit_price_usd: float | None  # None => orchestrator looks up historical price
    fee_usd: float
    exchange: str  # "bitget", "bybit", ...
    source: str  # "spot" | "earn" | "deposit" | "withdrawal" | "convert"
    external_id: str

    def comment(self) -> str:
        return f"{self.exchange}:{self.source}:{self.external_id}"


class PriceTracker:
    """Last-known USD price per coin, fed by observed spot trades.
    Used to value fees paid in coins like BGB that have no other price source."""

    def __init__(self) -> None:
        self._prices: dict[str, float] = {}

    def update(self, coin: str, usd_price: float) -> None:
        if usd_price > 0:
            self._prices[coin.upper()] = usd_price

    def get(self, coin: str) -> float | None:
        return self._prices.get(coin.upper())
