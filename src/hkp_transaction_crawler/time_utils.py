"""Time helpers shared by source snapshots and transaction filtering."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def _hong_kong_timezone() -> tzinfo:
    """Return Hong Kong time even on minimal Windows Python installations.

    Windows does not always ship the IANA time-zone database.  ``tzdata`` is
    therefore a package dependency, while the fixed UTC+08:00 fallback keeps
    the crawler usable if a constrained environment cannot load that database.
    Hong Kong has no daylight-saving-time transition to account for.
    """

    try:
        return ZoneInfo("Asia/Hong_Kong")
    except ZoneInfoNotFoundError:
        return timezone(timedelta(hours=8), name="Asia/Hong_Kong")


HONG_KONG_TIMEZONE = _hong_kong_timezone()


def hong_kong_today() -> date:
    """Return the program's current calendar date in Hong Kong time."""

    return datetime.now(HONG_KONG_TIMEZONE).date()
