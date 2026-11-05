from __future__ import annotations

import unittest

from hkp_transaction_crawler.geospatial_core import (
    GeoPoint,
    PoiRecord,
    RouteResult,
    compute_location_factors,
    count_pois_within_radius,
    haversine_km,
    nearest_route_distance_km,
)


ORIGIN = GeoPoint(22.300000, 114.170000)


def metres_north(latitude: float, metres: float) -> float:
    # Adequate for concise Hong Kong test fixtures; production uses haversine.
    return latitude + metres / 111_195


class GeospatialCoreTests(unittest.TestCase):
    def test_haversine_matches_one_degree_latitude(self) -> None:
        result = haversine_km(GeoPoint(0, 0), GeoPoint(1, 0))
        self.assertAlmostEqual(result, 111.195, places=3)

    def test_radius_count_deduplicates_only_when_requested(self) -> None:
        shared_stop_one = PoiRecord("bus-1", metres_north(ORIGIN.latitude, 200), ORIGIN.longitude)
        shared_stop_two = PoiRecord("gmb-1", metres_north(ORIGIN.latitude, 212), ORIGIN.longitude)
        distinct_stop = PoiRecord("tram-1", metres_north(ORIGIN.latitude, 300), ORIGIN.longitude)
        outside = PoiRecord("far", metres_north(ORIGIN.latitude, 1_100), ORIGIN.longitude)
        pois = [shared_stop_one, shared_stop_two, distinct_stop, outside]

        self.assertEqual(count_pois_within_radius(ORIGIN, pois), 3)
        self.assertEqual(
            count_pois_within_radius(ORIGIN, pois, deduplicate_within_meters=20),
            2,
        )

    def test_physical_key_merges_source_rows_even_when_coordinates_differ(self) -> None:
        first = PoiRecord("a", metres_north(ORIGIN.latitude, 100), ORIGIN.longitude, physical_key="shared")
        second = PoiRecord("b", metres_north(ORIGIN.latitude, 170), ORIGIN.longitude, physical_key="shared")
        self.assertEqual(
            count_pois_within_radius(ORIGIN, [first, second], deduplicate_within_meters=20),
            1,
        )

    def test_radius_count_merges_declared_institutions_without_merging_co_located_levels(self) -> None:
        morning = PoiRecord(
            "morning",
            metres_north(ORIGIN.latitude, 100),
            ORIGIN.longitude,
            physical_key="example|address|kindergarten",
        )
        afternoon = PoiRecord(
            "afternoon",
            metres_north(ORIGIN.latitude, 105),
            ORIGIN.longitude,
            physical_key="example|address|kindergarten",
        )
        primary = PoiRecord(
            "primary",
            metres_north(ORIGIN.latitude, 100),
            ORIGIN.longitude,
            physical_key="example|address|primary",
        )

        self.assertEqual(count_pois_within_radius(ORIGIN, [morning, afternoon, primary]), 3)
        self.assertEqual(
            count_pois_within_radius(
                ORIGIN,
                [morning, afternoon, primary],
                deduplicate_physical_keys=True,
            ),
            2,
        )

    def test_compute_location_factors_preserves_zero_and_missing_distinction(self) -> None:
        factors = compute_location_factors(
            ORIGIN,
            schools=[],
            public_transport_stops=[],
            hk8_routes=[RouteResult("hku", None), RouteResult("cuhk", 3_250)],
        )
        self.assertEqual(factors.school_count_within_1km, 0)
        self.assertEqual(factors.public_transport_stops_within_1km, 0)
        self.assertEqual(factors.nearest_hk8_university_distance_km, 3.25)

    def test_missing_origin_or_routes_produces_blanks(self) -> None:
        missing = compute_location_factors(
            None,
            schools=[],
            public_transport_stops=[],
            hk8_routes=[RouteResult("hku", 1_000)],
        )
        self.assertIsNone(missing.school_count_within_1km)
        self.assertIsNone(missing.public_transport_stops_within_1km)
        self.assertIsNone(missing.nearest_hk8_university_distance_km)
        self.assertIsNone(nearest_route_distance_km([]))

