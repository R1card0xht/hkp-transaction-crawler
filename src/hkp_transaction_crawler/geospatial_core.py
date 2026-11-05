"""Offline, deterministic calculations for location-based housing factors.

This module deliberately does not download data or call Google services.  The
caller supplies already-normalized official POIs and successful route results.
That separation keeps the metric definitions testable and makes a failed data
source distinguishable from a real zero nearby-facility count.
"""

from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, isfinite, radians, sin, sqrt
from typing import Iterable


EARTH_RADIUS_KM = 6_371.0088
DEFAULT_RADIUS_KM = 1.0
PUBLIC_TRANSPORT_DEDUPLICATION_METERS = 20.0


@dataclass(frozen=True, slots=True)
class GeoPoint:
    """A WGS84 coordinate used as an origin or a point-of-interest location."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        _validate_coordinate(self.latitude, self.longitude)


@dataclass(frozen=True, slots=True)
class PoiRecord:
    """A source-recorded point of interest.

    ``source_id`` identifies one source record. ``physical_key`` is optional
    and may be shared by records that the upstream source already identifies as
    the same physical stop.  It is especially useful for public transport.
    """

    source_id: str
    latitude: float
    longitude: float
    name: str | None = None
    category: str | None = None
    physical_key: str | None = None

    def __post_init__(self) -> None:
        if not self.source_id.strip():
            raise ValueError("PoiRecord.source_id must not be blank")
        _validate_coordinate(self.latitude, self.longitude)

    @property
    def point(self) -> GeoPoint:
        return GeoPoint(self.latitude, self.longitude)


@dataclass(frozen=True, slots=True)
class RouteResult:
    """A route response for one known destination.

    ``distance_meters=None`` represents a failed, unavailable, or otherwise
    unusable route. A numeric zero remains a valid result.
    """

    destination_id: str
    distance_meters: float | None
    duration_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.destination_id.strip():
            raise ValueError("RouteResult.destination_id must not be blank")
        _validate_non_negative_optional(self.distance_meters, "distance_meters")
        _validate_non_negative_optional(self.duration_seconds, "duration_seconds")


@dataclass(frozen=True, slots=True)
class LocationFactors:
    """Offline-computable location factors used during internal enrichment."""

    school_count_within_1km: int | None
    public_transport_stops_within_1km: int | None
    nearest_hk8_university_distance_km: float | None


def haversine_km(first: GeoPoint, second: GeoPoint) -> float:
    """Return straight-line great-circle distance in kilometres."""

    latitude_delta = radians(second.latitude - first.latitude)
    longitude_delta = radians(second.longitude - first.longitude)
    latitude_one = radians(first.latitude)
    latitude_two = radians(second.latitude)
    haversine = (
        sin(latitude_delta / 2) ** 2
        + cos(latitude_one) * cos(latitude_two) * sin(longitude_delta / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * asin(sqrt(haversine))


def count_pois_within_radius(
    origin: GeoPoint | None,
    pois: Iterable[PoiRecord] | None,
    *,
    radius_km: float = DEFAULT_RADIUS_KM,
    deduplicate_within_meters: float | None = None,
    deduplicate_physical_keys: bool = False,
) -> int | None:
    """Count unique POI records inside an inclusive straight-line radius.

    ``None`` indicates the origin or the source data was unavailable. An empty
    iterable is a valid, observed count of zero. Exact repeated source IDs are
    never double-counted. When ``deduplicate_physical_keys`` is true, rows
    that explicitly declare the same physical/institution key count once.
    ``deduplicate_within_meters`` instead clusters nearby points and is used
    for the public-transit factor, not the school factor.
    """

    _validate_positive(radius_km, "radius_km")
    if deduplicate_within_meters is not None:
        _validate_non_negative(deduplicate_within_meters, "deduplicate_within_meters")
    if origin is None or pois is None:
        return None

    in_radius: list[PoiRecord] = []
    seen_source_ids: set[str] = set()
    seen_physical_keys: set[str] = set()
    for poi in pois:
        if poi.source_id in seen_source_ids:
            continue
        seen_source_ids.add(poi.source_id)
        if haversine_km(origin, poi.point) > radius_km:
            continue
        physical_key = (poi.physical_key or "").strip()
        if deduplicate_physical_keys and physical_key:
            if physical_key in seen_physical_keys:
                continue
            seen_physical_keys.add(physical_key)
        in_radius.append(poi)

    if deduplicate_within_meters is None:
        return len(in_radius)
    return _count_physical_clusters(in_radius, deduplicate_within_meters)


def nearest_successful_route(routes: Iterable[RouteResult] | None) -> RouteResult | None:
    """Return the shortest route with a valid distance, or ``None`` if absent."""

    if routes is None:
        return None
    successful = [route for route in routes if route.distance_meters is not None]
    if not successful:
        return None
    return min(successful, key=lambda route: route.distance_meters or 0.0)


def nearest_route_distance_km(routes: Iterable[RouteResult] | None) -> float | None:
    """Return the nearest successful route distance in kilometres."""

    nearest = nearest_successful_route(routes)
    if nearest is None or nearest.distance_meters is None:
        return None
    return nearest.distance_meters / 1_000


def compute_location_factors(
    origin: GeoPoint | None,
    schools: Iterable[PoiRecord] | None,
    public_transport_stops: Iterable[PoiRecord] | None,
    hk8_routes: Iterable[RouteResult] | None,
) -> LocationFactors:
    """Compute agreed location factors from already-supplied source records.

    Schools count distinct formal institutions within one kilometre, merging
    repeated A.M./P.M./whole-day records through their declared institution
    key. Public transport counts physical stop locations inside the same
    radius, merging points within 20 metres. The university metric is the
    shortest successful Google Routes distance among the eight supplied main-
    campus destinations.
    """

    if origin is None:
        return LocationFactors(None, None, None)
    return LocationFactors(
        school_count_within_1km=count_pois_within_radius(
            origin,
            schools,
            radius_km=DEFAULT_RADIUS_KM,
            deduplicate_physical_keys=True,
        ),
        public_transport_stops_within_1km=count_pois_within_radius(
            origin,
            public_transport_stops,
            radius_km=DEFAULT_RADIUS_KM,
            deduplicate_within_meters=PUBLIC_TRANSPORT_DEDUPLICATION_METERS,
        ),
        nearest_hk8_university_distance_km=nearest_route_distance_km(hk8_routes),
    )


def _count_physical_clusters(pois: list[PoiRecord], threshold_meters: float) -> int:
    if not pois:
        return 0

    parent = list(range(len(pois)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for left_index, left in enumerate(pois):
        for right_index in range(left_index + 1, len(pois)):
            right = pois[right_index]
            same_declared_location = (
                left.physical_key is not None
                and left.physical_key.strip() != ""
                and left.physical_key == right.physical_key
            )
            close_coordinates = haversine_km(left.point, right.point) * 1_000 <= threshold_meters
            if same_declared_location or close_coordinates:
                union(left_index, right_index)

    return len({find(index) for index in range(len(pois))})


def _validate_coordinate(latitude: float, longitude: float) -> None:
    if not isfinite(latitude) or not isfinite(longitude):
        raise ValueError("coordinates must be finite numbers")
    if not -90 <= latitude <= 90:
        raise ValueError("latitude must be between -90 and 90")
    if not -180 <= longitude <= 180:
        raise ValueError("longitude must be between -180 and 180")


def _validate_positive(value: float, name: str) -> None:
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")


def _validate_non_negative(value: float, name: str) -> None:
    if not isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")


def _validate_non_negative_optional(value: float | None, name: str) -> None:
    if value is not None:
        _validate_non_negative(value, name)

