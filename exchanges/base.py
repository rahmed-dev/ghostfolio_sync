import os
import time
from abc import ABC, abstractmethod

from activity import Activity

# Default 24h. Override with LOOKBACK_HOURS env var (e.g. 2160 = 90d for a one-time backfill).
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "24"))
LOOKBACK_WINDOW_MS = LOOKBACK_HOURS * 3600 * 1000


class Exchange(ABC):
    name: str
    account_name: str

    def get_since_ms(self) -> int:
        return int(time.time() * 1000) - LOOKBACK_WINDOW_MS

    @abstractmethod
    def fetch_activities(self, since_ms: int) -> list[Activity]:
        ...

    @abstractmethod
    def fetch_current_balances(self) -> dict[str, float]:
        ...
