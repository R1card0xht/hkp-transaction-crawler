from __future__ import annotations

import unittest
from urllib.error import HTTPError
from unittest.mock import patch

from hkp_transaction_crawler.google_maps import GeoPoint, GoogleMapsClient, GoogleMapsLimitReached, haversine_km


class GoogleMapsTests(unittest.TestCase):
    def test_haversine_for_identical_points_is_zero(self) -> None:
        point = GeoPoint(22.3, 114.2)
        self.assertAlmostEqual(haversine_km(point, point), 0.0, places=9)

    def test_request_cap_stops_before_second_network_request(self) -> None:
        client = GoogleMapsClient("test-key", max_requests=1)
        client._consume_request()
        with self.assertRaises(GoogleMapsLimitReached):
            client._consume_request()

    def test_http_429_is_a_resumable_limit_signal(self) -> None:
        client = GoogleMapsClient("test-key", max_requests=10)
        with patch(
            "hkp_transaction_crawler.google_maps.urlopen",
            side_effect=HTTPError("https://example.invalid", 429, "rate", {}, None),
        ):
            with self.assertRaises(GoogleMapsLimitReached):
                client.nearby_search(GeoPoint(22.3, 114.2), included_types=["shopping_mall"])

