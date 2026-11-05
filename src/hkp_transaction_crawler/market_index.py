"""Official HKP Market Insight regional monthly price-index loader.

The public Market Insight page exposes its chart data in the same Next.js page
payload that a normal browser receives.  The output column retains the user's
chosen name, ``district_price_index``, but the underlying published series is
regional (Hong Kong / Kowloon / New Territories), not a neighbourhood index.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .client import ClientError, HKPClient
from .parser import ParseError, extract_next_data
from .time_utils import hong_kong_today


MARKET_INSIGHT_URL = "https://www.hkp.com.hk/zh-cn/market-insight"
_SERIES_BY_REGION = {
    "hong_kong": "mr_index_hk",
    "kowloon": "mr_index_kln",
    "new_territories": "mr_index_nt",
}


class MarketIndexError(RuntimeError):
    """The public Market Insight index data could not be used."""


@dataclass(frozen=True, slots=True)
class IndexPoint:
    month: str  # YYYY-MM
    hong_kong: float | None
    kowloon: float | None
    new_territories: float | None


@dataclass(frozen=True, slots=True)
class MarketIndexSnapshot:
    snapshot_date: str
    points: tuple[IndexPoint, ...]

    def value_for(self, region: str | None, transaction_date: str | None) -> float | None:
        """Return the matching formal monthly regional value, or prior month.

        The page can expose a provisional/latest incomplete month.  By looking
        up the latest point at or before the transaction month, the current
        incomplete month naturally falls back to the prior formally available
        monthly value when it is absent from the published data.
        """

        target_month = _as_month(transaction_date)
        series = _region_series(region)
        if target_month is None or series is None:
            return None
        candidates = [point for point in self.points if point.month <= target_month]
        if not candidates:
            return None
        value_name = {
            "mr_index_hk": "hong_kong",
            "mr_index_kln": "kowloon",
            "mr_index_nt": "new_territories",
        }[series]
        for point in reversed(candidates):
            value = getattr(point, value_name)
            if value is not None:
                return value
        return None


class MarketIndexLoader:
    """Fetch/cache the public regional monthly index once per source snapshot."""

    def __init__(
        self,
        cache_dir: str | Path,
        client: HKPClient,
        *,
        refresh: bool = False,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.client = client
        self.refresh = refresh

    def load_current(self) -> MarketIndexSnapshot:
        # Keep a cache for one current-source calendar day. A subsequent day
        # reads the public page again so its output remains a run-time snapshot.
        target = self.cache_dir / hong_kong_today().isoformat() / "hkp_market_index.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and not self.refresh:
            try:
                return snapshot_from_payload(json.loads(target.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError, MarketIndexError):
                # A corrupted local cache must not prevent a fresh public read.
                pass

        try:
            result = self.client.fetch_page(MARKET_INSIGHT_URL)
            payload = extract_next_data(result.text)
        except (ClientError, ParseError, OSError) as exc:
            raise MarketIndexError("无法读取香港置业 Market Insight 区域月度指数。") from exc
        snapshot = snapshot_from_payload(payload)
        target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return snapshot


def snapshot_from_payload(payload: Any, *, snapshot_date: str | None = None) -> MarketIndexSnapshot:
    """Extract the public ``mrIndex`` chart list from a Next.js payload."""

    rows = _find_mr_index_rows(payload)
    points: list[IndexPoint] = []
    for row in rows:
        if not isinstance(row, dict) or _is_weekly(row):
            continue
        month = _row_month(row)
        if month is None:
            continue
        values = {
            key: _number(_lookup_case_insensitive(row, key))
            for key in _SERIES_BY_REGION.values()
        }
        if all(value is None for value in values.values()):
            continue
        points.append(
            IndexPoint(
                month=month,
                hong_kong=values["mr_index_hk"],
                kowloon=values["mr_index_kln"],
                new_territories=values["mr_index_nt"],
            )
        )
    if not points:
        raise MarketIndexError("Market Insight 页面未包含可用的区域月度指数。")
    # Keep the last occurrence if a page payload contains duplicated chart rows.
    unique = {point.month: point for point in points}
    return MarketIndexSnapshot(
        snapshot_date=snapshot_date or hong_kong_today().isoformat(),
        points=tuple(unique[month] for month in sorted(unique)),
    )


def _find_mr_index_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).casefold() == "mrindex" and isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        for nested in value.values():
            found = _find_mr_index_rows(nested)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_mr_index_rows(nested)
            if found:
                return found
    return []


def _is_weekly(row: dict[str, Any]) -> bool:
    for key in ("weekly", "is_weekly", "isWeekly", "frequency", "period_type", "periodType"):
        value = _lookup_case_insensitive(row, key)
        if value is True:
            return True
        if isinstance(value, str) and value.casefold() in {"weekly", "week", "true", "1"}:
            return True
    return False


def _row_month(row: dict[str, Any]) -> str | None:
    for key in ("month", "date", "period", "tx_month", "transaction_month", "x"):
        value = _lookup_case_insensitive(row, key)
        result = _as_formal_month(value, row)
        if result:
            return result
    return None


def _as_month(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    matched = re.match(r"^(\d{4})[-/]?(\d{2})", text)
    if matched:
        year, month = int(matched.group(1)), int(matched.group(2))
        if 1 <= month <= 12:
            return f"{year:04d}-{month:02d}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m")
    except ValueError:
        return None


def _as_formal_month(value: Any, row: dict[str, Any]) -> str | None:
    """Normalize only a declared/formal monthly observation.

    A chart payload can contain daily or weekly companion points.  A bare
    non-first-of-month date has no reliable monthly meaning, so it is excluded
    unless the record itself explicitly says it is monthly (or non-weekly).
    """

    if value is None:
        return None
    text = str(value).strip()
    monthly_only = re.fullmatch(r"(\d{4})[-/](\d{1,2})", text)
    if monthly_only:
        year, month = int(monthly_only.group(1)), int(monthly_only.group(2))
        return f"{year:04d}-{month:02d}" if 1 <= month <= 12 else None

    dated = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if dated:
        year, month, day = (int(part) for part in dated.groups())
        if not 1 <= month <= 12 or not 1 <= day <= 31:
            return None
        if day == 1 or _declares_monthly(row):
            return f"{year:04d}-{month:02d}"
        return None
    return _as_month(value)


def _declares_monthly(row: dict[str, Any]) -> bool:
    for key in ("frequency", "period_type", "periodType", "granularity"):
        value = _lookup_case_insensitive(row, key)
        if isinstance(value, str) and value.casefold() in {"monthly", "month"}:
            return True
    for key in ("weekly", "is_weekly", "isWeekly"):
        if _lookup_case_insensitive(row, key) is False:
            return True
    return False


def _region_series(region: str | None) -> str | None:
    normalized = (region or "").strip().casefold()
    if not normalized:
        return None
    if any(token in normalized for token in ("九龙", "九龍", "kowloon")):
        return "mr_index_kln"
    if any(token in normalized for token in ("新界", "new territories", "newterritories")):
        return "mr_index_nt"
    # HKP transaction payloads label Hong Kong Island as either \"港岛\"
    # (simplified) or \"港島\" (traditional), rather than \"香港\".
    if any(token in normalized for token in ("香港", "港岛", "港島", "hong kong", "hongkong")):
        return "mr_index_hk"
    return None


def _lookup_case_insensitive(row: dict[str, Any], sought: str) -> Any:
    sought_normalized = sought.casefold().replace("_", "")
    for key, value in row.items():
        if str(key).casefold().replace("_", "") == sought_normalized:
            return value
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
