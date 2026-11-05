from __future__ import annotations

import unittest

from hkp_transaction_crawler.final_enricher import FinalRecordEnricher
from hkp_transaction_crawler.market_index import IndexPoint, MarketIndexSnapshot
from hkp_transaction_crawler.models import TransactionRecord


class _FakeGeographic:
    google_maps = None

    class snapshot:
        snapshot_date = "2026-11-01"

    def enrich(self, record: TransactionRecord) -> TransactionRecord:
        return record


class FinalEnricherTests(unittest.TestCase):
    def test_applies_regional_index_before_geographic_step(self) -> None:
        index = MarketIndexSnapshot(
            snapshot_date="2026-11-01",
            points=(IndexPoint("2023-01", 100.0, 101.0, 102.0),),
        )
        enricher = FinalRecordEnricher(_FakeGeographic(), index)
        record = TransactionRecord(
            record_id="I1",
            source_url="https://www.hkp.com.hk/zh-cn/transaction/example-I1",
            region="九龙",
            transaction_date="2023-01-31",
        )

        result = enricher.enrich(record)

        self.assertEqual(result.district_price_index, 101.0)
        self.assertEqual(enricher.geo_snapshot_date, "2026-11-01")

