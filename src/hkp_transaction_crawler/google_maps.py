"""Small Google Maps Platform client for derived accessibility metrics.

Only derived durations, distances and counts are persisted by the application.
The API key is read from ``GOOGLE_MAPS_API_KEY`` at runtime and is never
written to logs, CSV, Excel or SQLite.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .geospatial_core import GeoPoint, haversine_km


class GoogleMapsError(RuntimeError):
    """A Google Maps Platform request could not produce a usable result."""


class GoogleMapsLimitReached(GoogleMapsError):
    """The configured per-run request cap has been reached."""


@dataclass(frozen=True)
class PlaceCandidate:
    place_id: str
    name: str | None
    point: GeoPoint
    primary_type: str | None = None


@dataclass(frozen=True)
class WalkingRoute:
    duration_minutes: float
    distance_km: float


class GoogleMapsClient:
    """Use Places and Routes APIs with a hard, request-count cap.

    The cap controls HTTP requests, not Google billing elements.  Configure a
    Google Cloud budget/alert as the authoritative financial control.
    """

    _PLACES_NEARBY_URL = "https://places.googleapis.com/v1/places:searchNearby"
    _PLACES_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
    _ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"

    def __init__(
        self,
        api_key: str,
        *,
        max_requests: int = 100_000,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not api_key or api_key == "YOUR_GOOGLE_ROUTES_API_KEY":
            raise GoogleMapsError(
                "请设置有效的 GOOGLE_MAPS_API_KEY，并在同一项目中启用 Routes API 与 Places API。"
            )
        if max_requests < 1:
            raise ValueError("max_requests 必须至少为 1。")
        self._api_key = api_key
        self.max_requests = max_requests
        self.timeout_seconds = timeout_seconds
        self.request_count = 0

    @classmethod
    def from_environment(
        cls,
        *,
        max_requests: int = 100_000,
        timeout_seconds: float = 30.0,
    ) -> "GoogleMapsClient":
        return cls(
            os.environ.get("GOOGLE_MAPS_API_KEY", ""),
            max_requests=max_requests,
            timeout_seconds=timeout_seconds,
        )

    @property
    def requests_remaining(self) -> int:
        return self.max_requests - self.request_count

    def nearby_search(
        self,
        origin: GeoPoint,
        *,
        included_types: Iterable[str],
        radius_meters: float = 10_000,
        max_result_count: int = 20,
    ) -> list[PlaceCandidate]:
        """Return nearby POIs matching Google primary place types.

        This method is used for nearest shopping malls and major medical
        facilities. It deliberately does not persist raw Places payloads.
        """

        types = [item for item in included_types if item]
        if not types:
            return []
        payload = {
            "includedTypes": types,
            "maxResultCount": max(1, min(max_result_count, 20)),
            "locationRestriction": {
                "circle": {
                    "center": {"latitude": origin.latitude, "longitude": origin.longitude},
                    "radius": float(radius_meters),
                }
            },
            "rankPreference": "DISTANCE",
        }
        response = self._post_json(
            self._PLACES_NEARBY_URL,
            payload,
            field_mask="places.id,places.displayName,places.location,places.primaryType",
        )
        return _place_candidates(response)

    def text_search(
        self,
        origin: GeoPoint,
        *,
        query: str,
        radius_meters: float = 10_000,
        max_result_count: int = 20,
    ) -> list[PlaceCandidate]:
        """Search formal facility names near an origin using Places Text Search.

        This is intentionally used only for the narrow medical-facility
        definition, where a type-only search would miss government clinics or
        comprehensive medical centres.  Returned Place payloads remain
        transient and are not saved.
        """

        if not query.strip():
            return []
        payload = {
            "textQuery": query.strip(),
            "maxResultCount": max(1, min(max_result_count, 20)),
            "locationBias": {
                "circle": {
                    "center": {"latitude": origin.latitude, "longitude": origin.longitude},
                    "radius": float(radius_meters),
                }
            },
            "rankPreference": "DISTANCE",
        }
        response = self._post_json(
            self._PLACES_TEXT_URL,
            payload,
            field_mask="places.id,places.displayName,places.location,places.primaryType",
        )
        return _place_candidates(response)

    def walking_route(self, origin: GeoPoint, destination: GeoPoint) -> WalkingRoute | None:
        """Calculate a walking route. A no-route response returns ``None``."""

        payload = {
            "origin": _route_waypoint(origin),
            "destination": _route_waypoint(destination),
            "travelMode": "WALK",
            "units": "METRIC",
        }
        response = self._post_json(
            self._ROUTES_URL,
            payload,
            field_mask="routes.duration,routes.distanceMeters",
        )
        routes = _as_list(response.get("routes"))
        if not routes:
            return None
        route = _as_dict(routes[0])
        seconds = _duration_seconds(_as_text(route.get("duration")))
        meters = _as_float(route.get("distanceMeters"))
        if seconds is None or meters is None:
            return None
        return WalkingRoute(duration_minutes=seconds / 60.0, distance_km=meters / 1000.0)

    def nearest_walking_route(
        self,
        origin: GeoPoint,
        candidates: Iterable[GeoPoint],
        *,
        candidate_limit: int = 3,
        selection_metric: str = "duration",
    ) -> WalkingRoute | None:
        """Route to only the closest aerial candidates, then choose min walk.

        The pre-filter lowers request volume while retaining robust results in
        normal urban street networks. It is particularly useful for station
        entrances and eight-university campus candidates.
        """

        ranked = sorted(
            (candidate for candidate in candidates),
            key=lambda point: haversine_km(origin, point),
        )[: max(1, candidate_limit)]
        routes = [self.walking_route(origin, point) for point in ranked]
        valid = [route for route in routes if route is not None]
        if not valid:
            return None
        if selection_metric == "duration":
            return min(valid, key=lambda route: route.duration_minutes)
        if selection_metric == "distance":
            return min(valid, key=lambda route: route.distance_km)
        raise ValueError("selection_metric 必须是 duration 或 distance。")

    def _post_json(self, url: str, payload: dict[str, Any], *, field_mask: str) -> dict[str, Any]:
        self._consume_request()
        data_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": self._api_key,
            "X-Goog-FieldMask": field_mask,
        }
        retries = 3
        decoded = ""
        for attempt in range(retries + 1):
            request = Request(url, data=data_bytes, headers=headers, method="POST")
            try:
                with urlopen(request, timeout=self.timeout_seconds) as response:
                    decoded = response.read().decode("utf-8")
                break
            except HTTPError as exc:
                # Never include a request URL or the key in the error message.
                if exc.code in {401, 403}:
                    raise GoogleMapsError(
                        "Google Maps API 拒绝请求；请检查 API Key、已启用 API 与限制条件。"
                    ) from exc
                if exc.code == 429:
                    # Treat upstream throttling like the local per-run cap so the
                    # command layer can export committed work and resume later.
                    raise GoogleMapsLimitReached(
                        "Google Maps API 已限流；程序已停止，稍后可从断点续跑。"
                    ) from exc
                if exc.code >= 500 and attempt < retries:
                    time.sleep(min(8.0, 1.5 * (2**attempt)))
                    continue
                raise GoogleMapsError(f"Google Maps API 请求失败（HTTP {exc.code}）。") from exc
            except (URLError, TimeoutError, OSError, HTTPException) as exc:
                if attempt < retries:
                    time.sleep(min(8.0, 1.5 * (2**attempt)))
                    continue
                raise GoogleMapsError("无法连接 Google Maps API。") from exc
        try:
            parsed = json.loads(decoded)
        except json.JSONDecodeError as exc:
            raise GoogleMapsError("Google Maps API 返回了无效 JSON。") from exc
        if not isinstance(parsed, dict):
            raise GoogleMapsError("Google Maps API 返回格式异常。")
        return parsed

    def _consume_request(self) -> None:
        if self.request_count >= self.max_requests:
            raise GoogleMapsLimitReached(
                f"已达到本次运行 Google API 上限 {self.max_requests}；可稍后从 SQLite 断点续跑。"
            )
        self.request_count += 1


def _place_candidates(response: dict[str, Any]) -> list[PlaceCandidate]:
    candidates: list[PlaceCandidate] = []
    for item in _as_list(response.get("places")):
        if not isinstance(item, dict):
            continue
        location = _as_dict(item.get("location"))
        latitude = _as_float(location.get("latitude"))
        longitude = _as_float(location.get("longitude"))
        place_id = _as_text(item.get("id"))
        if latitude is None or longitude is None or not place_id:
            continue
        name = _as_text(_as_dict(item.get("displayName")).get("text"))
        candidates.append(
            PlaceCandidate(
                place_id=place_id,
                name=name,
                point=GeoPoint(latitude, longitude),
                primary_type=_as_text(item.get("primaryType")),
            )
        )
    return candidates


def _route_waypoint(point: GeoPoint) -> dict[str, Any]:
    return {"location": {"latLng": {"latitude": point.latitude, "longitude": point.longitude}}}


def _duration_seconds(value: str | None) -> float | None:
    if not value or not value.endswith("s"):
        return None
    try:
        return float(value[:-1])
    except ValueError:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None

