from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from hkp_transaction_crawler.time_utils import HONG_KONG_TIMEZONE


class HongKongTimeTests(unittest.TestCase):
    def test_hong_kong_timezone_is_utc_plus_eight(self) -> None:
        instant = datetime(2026, 1, 1, tzinfo=timezone.utc)
        self.assertEqual(instant.astimezone(HONG_KONG_TIMEZONE).utcoffset(), timedelta(hours=8))

