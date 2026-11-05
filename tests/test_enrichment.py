from __future__ import annotations

import unittest

from hkp_transaction_crawler.enrichment import GeoEnricher
from hkp_transaction_crawler.geospatial_core import PoiRecord
from hkp_transaction_crawler.google_maps import (
    GeoPoint,
    GoogleMapsLimitReached,
    PlaceCandidate,
    WalkingRoute,
)
from hkp_transaction_crawler.models import TransactionRecord
from hkp_transaction_crawler.official_sources import OfficialPoiSnapshot


ORIGIN_LAT = 22.300000
ORIGIN_LON = 114.170000


def metres_north(metres: float) -> float:
    return ORIGIN_LAT + metres / 111_195


class FakeGoogleMaps:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, ...]] = []
        self.text_calls: list[str] = []
        self.route_calls: list[tuple[int, int, str]] = []
        self.raise_limit = False

    def nearby_search(self, origin, *, included_types, radius_meters=10_000, max_result_count=20):
        types = tuple(included_types)
        self.search_calls.append(types)
        if "shopping_mall" in types:
            return [PlaceCandidate("mall", "商场", GeoPoint(metres_north(200), ORIGIN_LON))]
        return [PlaceCandidate("fallback-mtr", "MTR Station", GeoPoint(metres_north(100), ORIGIN_LON))]

    def text_search(self, origin, *, query, radius_meters=10_000, max_result_count=20):
        self.text_calls.append(query)
        if query == "hospital":
            return [PlaceCandidate("hospital", "医院", GeoPoint(metres_north(300), ORIGIN_LON))]
        if query == "government clinic":
            # Duplicate hospital place IDs from multiple searches must not add
            # duplicate route candidates.
            return [PlaceCandidate("hospital", "医院", GeoPoint(metres_north(300), ORIGIN_LON))]
        return [PlaceCandidate("dentist", "Example Dentist", GeoPoint(metres_north(100), ORIGIN_LON), "dentist")]

    def nearest_walking_route(self, origin, candidates, *, candidate_limit=3, selection_metric="duration"):
        if self.raise_limit:
            raise GoogleMapsLimitReached("test request cap")
        points = list(candidates)
        self.route_calls.append((len(points), candidate_limit, selection_metric))
        if candidate_limit == 3 and len(points) == 8:
            return WalkingRoute(duration_minutes=31.0, distance_km=4.8)
        if candidate_limit == 3 and len(points) == 1 and points[0].latitude == metres_north(100):
            return WalkingRoute(duration_minutes=6.0, distance_km=0.4)
        if candidate_limit == 3 and len(points) == 1 and points[0].latitude == metres_north(200):
            return WalkingRoute(duration_minutes=5.0, distance_km=0.3)
        return WalkingRoute(duration_minutes=7.0, distance_km=0.5)


def snapshot(*, official_railway=True) -> OfficialPoiSnapshot:
    schools = [
        PoiRecord("school-1", metres_north(250), ORIGIN_LON),
        PoiRecord("school-far", metres_north(1_300), ORIGIN_LON),
    ]
    public_transport = [
        PoiRecord("bus-1", metres_north(350), ORIGIN_LON),
        PoiRecord("gmb-1", metres_north(360), ORIGIN_LON),
    ]
    railway = [PoiRecord("rail-1", metres_north(100), ORIGIN_LON)] if official_railway else None
    campuses = [
        PoiRecord(f"hk8-{index}", metres_north(2_000 + index), ORIGIN_LON)
        for index in range(8)
    ]
    return OfficialPoiSnapshot(
        snapshot_date="2026-11-01",
        schools=schools,
        public_transport_stops=public_transport,
        railway_accesses=railway,
        hk8_main_campuses=campuses,
    )


def record(*, latitude=ORIGIN_LAT, longitude=ORIGIN_LON) -> TransactionRecord:
    return TransactionRecord(
        record_id="I1",
        source_url="https://www.hkp.com.hk/zh-cn/transaction/example-I1",
        building_id="B1",
        latitude=latitude,
        longitude=longitude,
    )


class EnrichmentTests(unittest.TestCase):
    def test_enriches_all_finalized_geographic_factors(self) -> None:
        maps = FakeGoogleMaps()
        enriched = GeoEnricher(snapshot(), maps).enrich(record())

        self.assertEqual(enriched.school_count_within_1km, 1)
        # The two road-service records are within 20m and the rail station is
        # a separate physical location, so count is 2.
        self.assertEqual(enriched.public_transport_stops_within_1km, 2)
        self.assertEqual(enriched.mtr_walk_min, 6.0)
        self.assertEqual(enriched.mall_walk_min, 5.0)
        self.assertEqual(enriched.medical_walk_min, 7.0)
        self.assertEqual(maps.text_calls, ["hospital", "government clinic", "medical centre"])
        self.assertEqual(enriched.nearest_hk8_university_distance_km, 4.8)
        self.assertIn((8, 3, "distance"), maps.route_calls)
        self.assertEqual(enriched.geo_snapshot_date, "2026-11-01")
        self.assertEqual(enriched.geo_status, "ok")

    def test_uses_places_for_mtr_only_when_official_source_is_unavailable(self) -> None:
        maps = FakeGoogleMaps()
        enriched = GeoEnricher(snapshot(official_railway=False), maps).enrich(record())

        self.assertEqual(enriched.mtr_walk_min, 6.0)
        self.assertIn("official_railway_source_unavailable", enriched.geo_status or "")
        self.assertIn(("subway_station", "light_rail_station", "train_station"), maps.search_calls)

    def test_missing_coordinates_produces_blanks_without_maps_calls(self) -> None:
        maps = FakeGoogleMaps()
        enriched = GeoEnricher(snapshot(), maps).enrich(record(latitude=None, longitude=None))

        self.assertIsNone(enriched.school_count_within_1km)
        self.assertIsNone(enriched.mtr_walk_min)
        self.assertEqual(enriched.geo_status, "coordinate_missing")
        self.assertEqual(maps.search_calls, [])
        self.assertEqual(maps.route_calls, [])

    def test_keeps_google_cap_as_a_stop_signal(self) -> None:
        maps = FakeGoogleMaps()
        maps.raise_limit = True
        with self.assertRaises(GoogleMapsLimitReached):
            GeoEnricher(snapshot(), maps).enrich(record())

    def test_keeps_official_counts_when_google_is_unavailable(self) -> None:
        enriched = GeoEnricher(snapshot(), None).enrich(record())

        self.assertEqual(enriched.school_count_within_1km, 1)
        self.assertEqual(enriched.public_transport_stops_within_1km, 2)
        self.assertIsNone(enriched.mtr_walk_min)
        self.assertIsNone(enriched.mall_walk_min)
        self.assertIn("google_maps_unavailable", enriched.geo_status or "")

    def test_school_count_merges_sessions_but_keeps_distinct_education_levels(self) -> None:
        source_snapshot = snapshot()
        source_snapshot.schools = [
            PoiRecord("school-am", metres_north(120), ORIGIN_LON, physical_key="example|address|kindergarten"),
            PoiRecord("school-pm", metres_north(121), ORIGIN_LON, physical_key="example|address|kindergarten"),
            PoiRecord("school-primary", metres_north(120), ORIGIN_LON, physical_key="example|address|primary"),
        ]

        enriched = GeoEnricher(source_snapshot, None).enrich(record())

        self.assertEqual(enriched.school_count_within_1km, 2)

    def test_reuses_optional_coordinate_cache(self) -> None:
        maps = FakeGoogleMaps()
        cache = {}
        enricher = GeoEnricher(snapshot(), maps, cache_get=cache.get, cache_put=cache.__setitem__)

        first = enricher.enrich(record())
        route_count = len(maps.route_calls)
        second = enricher.enrich(record())

        self.assertEqual(first.mtr_walk_min, second.mtr_walk_min)
        self.assertEqual(route_count, len(maps.route_calls))
        self.assertEqual(len(cache), 1)
        self.assertTrue(next(iter(cache)).startswith("v2-school-institutions|"))

    def test_caches_a_source_unavailable_partial_result_with_source_state_key(self) -> None:
        maps = FakeGoogleMaps()
        cache = {}
        enricher = GeoEnricher(
            snapshot(official_railway=False),
            maps,
            cache_get=cache.get,
            cache_put=cache.__setitem__,
        )

        first = enricher.enrich(record())
        route_count = len(maps.route_calls)
        second = enricher.enrich(record())

        self.assertIn("official_railway_source_unavailable", first.geo_status or "")
        self.assertEqual(first.mtr_walk_min, second.mtr_walk_min)
        self.assertEqual(route_count, len(maps.route_calls))
        self.assertEqual(len(cache), 1)

