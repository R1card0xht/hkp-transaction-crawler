from __future__ import annotations

import unittest

from hkp_transaction_crawler.market_index import snapshot_from_payload


class MarketIndexTests(unittest.TestCase):
    def test_matches_region_and_falls_back_to_prior_available_month(self) -> None:
        payload = {
            "props": {
                "pageProps": {
                    "mrIndex": [
                        {
                            "date": "2023-01-01",
                            "mr_index_hk": "100.1",
                            "mr_index_kln": "101.2",
                            "mr_index_nt": "102.3",
                        },
                        {
                            "date": "2023-03-01",
                            "mr_index_hk": "103.1",
                            "mr_index_kln": "104.2",
                            "mr_index_nt": "105.3",
                        },
                        {
                            "date": "2023-03-08",
                            "weekly": True,
                            "mr_index_hk": "999.0",
                        },
                        {
                            # A companion weekly/daily point with no explicit
                            # weekly metadata must not overwrite March's
                            # formal monthly reading.
                            "date": "2023-03-15",
                            "mr_index_kln": "998.0",
                        },
                    ]
                }
            }
        }
        snapshot = snapshot_from_payload(payload, snapshot_date="2026-11-01")

        self.assertEqual(snapshot.snapshot_date, "2026-11-01")
        self.assertEqual(snapshot.value_for("九龙", "2023-03-20"), 104.2)
        self.assertEqual(snapshot.value_for("Hong Kong", "2023-02-15"), 100.1)
        self.assertEqual(snapshot.value_for("港岛", "2023-02-15"), 100.1)
        self.assertEqual(snapshot.value_for("港島", "2023-02-15"), 100.1)
        self.assertEqual(snapshot.value_for("香港岛", "2023-02-15"), 100.1)
        self.assertEqual(snapshot.value_for("Hong Kong Island", "2023-02-15"), 100.1)
        self.assertEqual(snapshot.value_for("新界", "2022-12-31"), None)
        self.assertEqual(snapshot.value_for(None, "2023-03-20"), None)

