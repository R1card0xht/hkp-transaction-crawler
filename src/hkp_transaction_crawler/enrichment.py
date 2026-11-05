"""Per-record geographic enrichment for the final Hong Kong housing schema.

The module joins a parsed transaction with current official POI snapshots and
derived Google Maps results.  It never persists raw Google Places content: the
returned record contains only the agreed numeric factors and internal status.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable, Iterable, Protocol

from .geospatial_core import (
    DEFAULT_RADIUS_KM,
    PUBLIC_TRANSPORT_DEDUPLICATION_METERS,
    GeoPoint as CoreGeoPoint,
    PoiRecord,
    count_pois_within_radius,
)
from .google_maps import (
    GeoPoint as GoogleGeoPoint,
    GoogleMapsError,
    GoogleMapsLimitReached,
    PlaceCandidate,
    WalkingRoute,
)
from .models import TransactionRecord
from .official_sources import OfficialPoiSnapshot


MALL_PLACE_TYPES = ("shopping_mall",)
# Text Search is intentionally used for the narrow, agreed major-facility
# definition: type-only nearby search can miss government clinics and medical
# centres. Individual doctors, dentists and pharmacies are later excluded.
MAJOR_MEDICAL_TEXT_QUERIES = ("hospital", "government clinic", "medical centre")
EXCLUDED_MEDICAL_PRIMARY_TYPES = {
    "dentist",
    "doctor",
    "medical_lab",
    "pharmacy",
    "physiotherapist",
    "veterinary_care",
}
EXCLUDED_MEDICAL_NAME_TOKENS = (
    "dentist",
    "doctor",
    "pharmacy",
    "veterinary",
    "animal hospital",
    "牙科",
    "藥房",
    "药房",
    "獸醫",
    "兽医",
    "動物",
    "动物",
)
MAJOR_MEDICAL_NAME_TOKENS = (
    "hospital",
    "醫院",
    "医院",
    "government clinic",
    "政府診所",
    "政府诊所",
    "general out-patient clinic",
    "普通科門診",
    "普通科门诊",
    "medical centre",
    "medical center",
    "醫療中心",
    "医疗中心",
)
# Used only when the official railway-access source is unavailable. Restricting
# the Places query avoids treating generic bus stops as MTR/Light Rail.
MTR_FALLBACK_PLACE_TYPES = ("subway_station", "light_rail_station", "train_station")
_GEO_FACTOR_CACHE_VERSION = "v2-school-institutions"


class GoogleMapsService(Protocol):
    """Subset of :class:`GoogleMapsClient` needed for geographic enrichment."""

    def nearby_search(
        self,
        origin: GoogleGeoPoint,
        *,
        included_types: Iterable[str],
        radius_meters: float = 10_000,
        max_result_count: int = 20,
    ) -> list[PlaceCandidate]: ...

    def text_search(
        self,
        origin: GoogleGeoPoint,
        *,
        query: str,
        radius_meters: float = 10_000,
        max_result_count: int = 20,
    ) -> list[PlaceCandidate]: ...

    def nearest_walking_route(
        self,
        origin: GoogleGeoPoint,
        candidates: Iterable[GoogleGeoPoint],
        *,
        candidate_limit: int = 3,
        selection_metric: str = "duration",
    ) -> WalkingRoute | None: ...


@dataclass(frozen=True, slots=True)
class LocationEnrichment:
    """Cacheable, record-independent location measures for one coordinate."""

    mtr_walk_min: float | None
    public_transport_stops_within_1km: int | None
    mall_walk_min: float | None
    school_count_within_1km: int | None
    nearest_hk8_university_distance_km: float | None
    medical_walk_min: float | None
    geo_snapshot_date: str
    geo_status: str

    def apply(self, record: TransactionRecord) -> TransactionRecord:
        """Return a copied record containing the finalized geographic factors."""

        return replace(
            record,
            mtr_walk_min=self.mtr_walk_min,
            public_transport_stops_within_1km=self.public_transport_stops_within_1km,
            mall_walk_min=self.mall_walk_min,
            school_count_within_1km=self.school_count_within_1km,
            nearest_hk8_university_distance_km=self.nearest_hk8_university_distance_km,
            medical_walk_min=self.medical_walk_min,
            geo_snapshot_date=self.geo_snapshot_date,
            geo_status=self.geo_status,
        )


CacheGet = Callable[[str], LocationEnrichment | None]
CachePut = Callable[[str, LocationEnrichment], None]


class GeoEnricher:
    """Enrich records using one current official snapshot and optional maps API.

    The optional cache callbacks should cache only :class:`LocationEnrichment`
    objects.  A cache key includes the snapshot date and coordinate, so a new
    current-source run cannot accidentally reuse a previous geography snapshot.
    """

    def __init__(
        self,
        snapshot: OfficialPoiSnapshot,
        google_maps: GoogleMapsService | None,
        *,
        cache_get: CacheGet | None = None,
        cache_put: CachePut | None = None,
    ) -> None:
        self.snapshot = snapshot
        self.google_maps = google_maps
        self.cache_get = cache_get
        self.cache_put = cache_put

    def enrich(self, record: TransactionRecord) -> TransactionRecord:
        """Return a new record with all agreed geographic factor values.

        Source/API failures leave only their affected factor blank.  A genuine
        zero count from a successfully loaded official source remains zero.  A
        Google request-cap exception intentionally propagates so the caller can
        stop and later resume rather than silently writing incomplete rows.
        """

        origin = _record_origin(record)
        if origin is None:
            return LocationEnrichment(
                mtr_walk_min=None,
                public_transport_stops_within_1km=None,
                mall_walk_min=None,
                school_count_within_1km=None,
                nearest_hk8_university_distance_km=None,
                medical_walk_min=None,
                geo_snapshot_date=self.snapshot.snapshot_date,
                geo_status="coordinate_missing",
            ).apply(record)

        key = self._cache_key(record, origin)
        if self.cache_get is not None:
            cached = self.cache_get(key)
            if cached is not None:
                return cached.apply(record)

        enrichment = self._calculate(origin)
        # A source-availability signature is part of the key below, so a
        # partial result caused by a temporarily unavailable official file can
        # be reused without repeatedly spending Google calls, yet is naturally
        # recalculated as soon as that source becomes available. Transient
        # Google failures are deliberately never cached.
        if self.cache_put is not None and _is_cacheable(enrichment):
            self.cache_put(key, enrichment)
        return enrichment.apply(record)

    def _calculate(self, origin: CoreGeoPoint) -> LocationEnrichment:
        statuses: list[str] = []
        school_count = self._school_count(origin, statuses)
        transit_count = self._public_transport_count(origin, statuses)
        mtr_minutes = self._mtr_walk_minutes(origin, statuses)
        university_distance = self._nearest_hk8_distance(origin, statuses)
        mall_minutes = self._nearby_walking_minutes(
            origin,
            place_types=MALL_PLACE_TYPES,
            factor_name="mall",
            statuses=statuses,
        )
        medical_minutes = self._medical_walk_minutes(origin, statuses)
        return LocationEnrichment(
            mtr_walk_min=mtr_minutes,
            public_transport_stops_within_1km=transit_count,
            mall_walk_min=mall_minutes,
            school_count_within_1km=school_count,
            nearest_hk8_university_distance_km=university_distance,
            medical_walk_min=medical_minutes,
            geo_snapshot_date=self.snapshot.snapshot_date,
            geo_status="ok" if not statuses else ";".join(_unique(statuses)),
        )

    def _school_count(self, origin: CoreGeoPoint, statuses: list[str]) -> int | None:
        count = count_pois_within_radius(
            origin,
            self.snapshot.schools,
            radius_km=DEFAULT_RADIUS_KM,
            deduplicate_physical_keys=True,
        )
        if count is None:
            statuses.append("schools_source_unavailable")
        return count

    def _public_transport_count(self, origin: CoreGeoPoint, statuses: list[str]) -> int | None:
        count = count_pois_within_radius(
            origin,
            self.snapshot.all_transport_stops,
            radius_km=DEFAULT_RADIUS_KM,
            deduplicate_within_meters=PUBLIC_TRANSPORT_DEDUPLICATION_METERS,
        )
        if count is None:
            statuses.append("public_transport_source_unavailable")
        return count

    def _mtr_walk_minutes(self, origin: CoreGeoPoint, statuses: list[str]) -> float | None:
        if self.google_maps is None:
            statuses.append("google_maps_unavailable")
            return None

        official_candidates = self.snapshot.railway_accesses
        if official_candidates is None:
            statuses.append("official_railway_source_unavailable")
            return self._mtr_google_fallback(origin, statuses)
        if not official_candidates:
            # A successfully read, but empty official source is not grounds for
            # a less precise Google fallback; preserve the missing distinction.
            statuses.append("official_railway_candidates_empty")
            return None

        route = self._route_to_pois(origin, official_candidates, candidate_limit=3, statuses=statuses, label="mtr")
        return route.duration_minutes if route is not None else None

    def _mtr_google_fallback(self, origin: CoreGeoPoint, statuses: list[str]) -> float | None:
        assert self.google_maps is not None
        try:
            places = self.google_maps.nearby_search(
                _to_google_point(origin),
                included_types=MTR_FALLBACK_PLACE_TYPES,
                radius_meters=50_000,
            )
        except GoogleMapsLimitReached:
            raise
        except GoogleMapsError:
            statuses.append("mtr_fallback_places_failed")
            return None
        if not places:
            statuses.append("mtr_fallback_not_found")
            return None
        route = self._route_to_places(origin, places, candidate_limit=3, statuses=statuses, label="mtr_fallback")
        return route.duration_minutes if route is not None else None

    def _nearest_hk8_distance(self, origin: CoreGeoPoint, statuses: list[str]) -> float | None:
        campuses = self.snapshot.hk8_main_campuses
        if campuses is None:
            statuses.append("hk8_source_unavailable")
            return None
        if not campuses:
            statuses.append("hk8_candidates_empty")
            return None
        if self.google_maps is None:
            statuses.append("google_maps_unavailable")
            return None
        # Evaluate the three aerially closest main campuses, then select the
        # shortest walking route. This remains robust in urban street networks
        # while respecting the agreed Google API request budget.
        route = self._route_to_pois(
            origin,
            campuses,
            candidate_limit=3,
            statuses=statuses,
            label="hk8",
            selection_metric="distance",
        )
        return route.distance_km if route is not None else None

    def _nearby_walking_minutes(
        self,
        origin: CoreGeoPoint,
        *,
        place_types: Iterable[str],
        factor_name: str,
        statuses: list[str],
    ) -> float | None:
        if self.google_maps is None:
            statuses.append("google_maps_unavailable")
            return None
        try:
            places = self.google_maps.nearby_search(
                _to_google_point(origin),
                included_types=place_types,
                radius_meters=10_000,
            )
        except GoogleMapsLimitReached:
            raise
        except GoogleMapsError:
            statuses.append(f"{factor_name}_places_failed")
            return None
        if not places:
            statuses.append(f"{factor_name}_not_found")
            return None
        route = self._route_to_places(origin, places, candidate_limit=3, statuses=statuses, label=factor_name)
        return route.duration_minutes if route is not None else None

    def _medical_walk_minutes(self, origin: CoreGeoPoint, statuses: list[str]) -> float | None:
        """Route to the closest major medical facility from combined text queries."""

        if self.google_maps is None:
            statuses.append("google_maps_unavailable")
            return None

        candidates: list[PlaceCandidate] = []
        try:
            for query in MAJOR_MEDICAL_TEXT_QUERIES:
                candidates.extend(
                    self.google_maps.text_search(
                        _to_google_point(origin),
                        query=query,
                        radius_meters=10_000,
                    )
                )
        except GoogleMapsLimitReached:
            raise
        except GoogleMapsError:
            statuses.append("medical_places_failed")
            return None

        major_facilities = _deduplicate_places(
            place for place in candidates if _is_major_medical_facility(place)
        )
        if not major_facilities:
            statuses.append("medical_not_found")
            return None
        route = self._route_to_places(
            origin,
            major_facilities,
            candidate_limit=3,
            statuses=statuses,
            label="medical",
        )
        return route.duration_minutes if route is not None else None

    def _route_to_pois(
        self,
        origin: CoreGeoPoint,
        pois: Iterable[PoiRecord],
        *,
        candidate_limit: int,
        statuses: list[str],
        label: str,
        selection_metric: str = "duration",
    ) -> WalkingRoute | None:
        return self._route_to_points(
            origin,
            (_to_google_point(poi.point) for poi in pois),
            candidate_limit=candidate_limit,
            statuses=statuses,
            label=label,
            selection_metric=selection_metric,
        )

    def _route_to_places(
        self,
        origin: CoreGeoPoint,
        places: Iterable[PlaceCandidate],
        *,
        candidate_limit: int,
        statuses: list[str],
        label: str,
        selection_metric: str = "duration",
    ) -> WalkingRoute | None:
        return self._route_to_points(
            origin,
            (place.point for place in places),
            candidate_limit=candidate_limit,
            statuses=statuses,
            label=label,
            selection_metric=selection_metric,
        )

    def _route_to_points(
        self,
        origin: CoreGeoPoint,
        candidates: Iterable[GoogleGeoPoint],
        *,
        candidate_limit: int,
        statuses: list[str],
        label: str,
        selection_metric: str = "duration",
    ) -> WalkingRoute | None:
        assert self.google_maps is not None
        try:
            route = self.google_maps.nearest_walking_route(
                _to_google_point(origin),
                candidates,
                candidate_limit=candidate_limit,
                selection_metric=selection_metric,
            )
        except GoogleMapsLimitReached:
            raise
        except GoogleMapsError:
            statuses.append(f"{label}_route_failed")
            return None
        if route is None:
            statuses.append(f"{label}_route_not_found")
        return route

    def _cache_key(self, record: TransactionRecord, origin: CoreGeoPoint) -> str:
        building = record.building_id or "no-building-id"
        source_state = "".join(
            (
                f"s{int(self.snapshot.schools is not None)}",
                f"t{int(self.snapshot.public_transport_stops is not None)}",
                f"r{int(self.snapshot.railway_accesses is not None)}",
                f"u{int(self.snapshot.hk8_main_campuses is not None)}",
            )
        )
        return (
            f"{_GEO_FACTOR_CACHE_VERSION}|{self.snapshot.snapshot_date}|{source_state}|{building}|"
            f"{origin.latitude:.6f}|{origin.longitude:.6f}"
        )


def enrich(
    record: TransactionRecord,
    snapshot: OfficialPoiSnapshot,
    google_maps: GoogleMapsService | None,
    *,
    cache_get: CacheGet | None = None,
    cache_put: CachePut | None = None,
) -> TransactionRecord:
    """Convenience function for one-off enrichment without managing a class."""

    return GeoEnricher(
        snapshot,
        google_maps,
        cache_get=cache_get,
        cache_put=cache_put,
    ).enrich(record)


def _record_origin(record: TransactionRecord) -> CoreGeoPoint | None:
    if record.latitude is None or record.longitude is None:
        return None
    try:
        return CoreGeoPoint(record.latitude, record.longitude)
    except ValueError:
        return None


def _to_google_point(point: CoreGeoPoint) -> GoogleGeoPoint:
    return GoogleGeoPoint(latitude=point.latitude, longitude=point.longitude)


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _is_cacheable(enrichment: LocationEnrichment) -> bool:
    """Whether a result contains no transient Google-side failure."""

    statuses = enrichment.geo_status.split(";") if enrichment.geo_status else []
    return not any(
        status == "google_maps_unavailable" or status.endswith("_failed")
        for status in statuses
    )


def _deduplicate_places(places: Iterable[PlaceCandidate]) -> list[PlaceCandidate]:
    """Preserve the first result for each Google Place ID across text queries."""

    seen: set[str] = set()
    result: list[PlaceCandidate] = []
    for place in places:
        if place.place_id not in seen:
            result.append(place)
            seen.add(place.place_id)
    return result


def _is_major_medical_facility(place: PlaceCandidate) -> bool:
    primary_type = (place.primary_type or "").casefold()
    if primary_type in EXCLUDED_MEDICAL_PRIMARY_TYPES:
        return False
    name = (place.name or "").casefold()
    if any(token in name for token in EXCLUDED_MEDICAL_NAME_TOKENS):
        return False
    return primary_type == "hospital" or any(token in name for token in MAJOR_MEDICAL_NAME_TOKENS)

