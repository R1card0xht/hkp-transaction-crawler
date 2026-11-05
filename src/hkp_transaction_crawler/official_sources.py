"""Current-snapshot loaders for the agreed Hong Kong official POI sources.

These source files are deliberately separate from Google Places content:

* Education Bureau formal-school data: school-count factor;
* Transport Department GTFS stops plus LandsD railway access POIs: public
  transport count and MTR/Light Rail candidate destinations;
* UGC campus data: the eight main-campus destination coordinates.

Downloads are cached with their run date.  If a source cannot be obtained, its
corresponding factor can remain blank rather than being imputed as zero.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .geospatial_core import PoiRecord
from .time_utils import hong_kong_today


SCHOOL_LOCATION_URL = (
    "https://www.edb.gov.hk/attachment/en/student-parents/sch-info/"
    "sch-search/sch-location-info/SCH_LOC_EDB.xlsx"
)
PUBLIC_TRANSIT_GTFS_URL = "https://static.data.gov.hk/td/pt-headway-tc/stops.txt"
# The GTFS stop feed covers bus and feeder-bus stop locations.  Transport
# Department's current Routes-and-Fares XML stop tables supply the additional
# scheduled GMB, tram and ferry physical stops in HK1980 grid coordinates.
PUBLIC_TRANSIT_AUXILIARY_STOP_URLS = {
    "gmb": "https://static.data.gov.hk/td/routes-fares-xml/STOP_GMB.xml",
    "tram": "https://static.data.gov.hk/td/routes-fares-xml/STOP_TRAM.xml",
    "ferry": "https://static.data.gov.hk/td/routes-fares-xml/STOP_FERRY.xml",
}
IGEO_COM_URL = (
    "https://open.hkmapservice.gov.hk/OpenData/directDownload?"
    "productName=iGeoCom&sheetName=iGeoCom&productFormat=GEOJSON"
)
UGC_HK8_URL = "https://www.ugcs.gov.hk/datagovhk/UGC-Funded-Institutions_TC.csv"

# Main campuses only, in the official UGC location dataset.  The file can gain
# auxiliary facilities over time, so accepting an arbitrary first eight rows
# would silently change the university-distance definition.
HK8_MAIN_CAMPUS_NAMES = {
    "hku": ("香港大學", "the university of hong kong", "university of hong kong"),
    "cuhk": ("香港中文大學", "the chinese university of hong kong"),
    "hkust": ("香港科技大學", "hong kong university of science and technology"),
    "polyu": ("香港理工大學", "the hong kong polytechnic university"),
    "cityu": ("香港城市大學", "city university of hong kong"),
    "hkbu": ("香港浸會大學", "hong kong baptist university"),
    "lnu": ("嶺南大學", "岭南大学", "lingnan university"),
    "eduhk": ("香港教育大學", "the education university of hong kong"),
}


class OfficialSourceError(RuntimeError):
    """An official data source was unavailable or structurally invalid."""


@dataclass(slots=True)
class OfficialPoiSnapshot:
    """POIs from one current-source snapshot and any recoverable failures."""

    snapshot_date: str
    schools: list[PoiRecord] | None = None
    public_transport_stops: list[PoiRecord] | None = None
    railway_accesses: list[PoiRecord] | None = None
    hk8_main_campuses: list[PoiRecord] | None = None
    source_errors: dict[str, str] = field(default_factory=dict)

    @property
    def all_transport_stops(self) -> list[PoiRecord] | None:
        if self.public_transport_stops is None or self.railway_accesses is None:
            return None
        return [*self.public_transport_stops, *self.railway_accesses]


class OfficialSourceLoader:
    """Download/cache official current-location files and normalize POIs."""

    def __init__(
        self,
        cache_dir: str | Path,
        *,
        timeout_seconds: float = 60.0,
        refresh: bool = False,
        fetcher: Callable[[str], bytes] | None = None,
    ) -> None:
        self.cache_dir = Path(cache_dir)
        self.timeout_seconds = timeout_seconds
        self.refresh = refresh
        self.fetcher = fetcher or self._download

    def load_current(self) -> OfficialPoiSnapshot:
        """Load all agreed sources, retaining independent failure details."""

        today = hong_kong_today().isoformat()
        snapshot = OfficialPoiSnapshot(snapshot_date=today)
        loaders = (
            ("schools", self._load_schools),
            ("public_transport_stops", self._load_public_transport),
            ("railway_accesses", self._load_railway_accesses),
            ("hk8_main_campuses", self._load_hk8_main_campuses),
        )
        for name, loader in loaders:
            try:
                setattr(snapshot, name, loader())
            except OfficialSourceError as exc:
                snapshot.source_errors[name] = str(exc)
        return snapshot

    def _load_schools(self) -> list[PoiRecord]:
        payload = self._cached_bytes("school_locations.xlsx", SCHOOL_LOCATION_URL)
        return parse_school_location_workbook(payload)

    def _load_public_transport(self) -> list[PoiRecord]:
        payload = self._cached_bytes("public_transport_stops.txt", PUBLIC_TRANSIT_GTFS_URL)
        stops = parse_gtfs_stops(payload)
        for mode, url in PUBLIC_TRANSIT_AUXILIARY_STOP_URLS.items():
            auxiliary = self._cached_bytes(f"public_transport_{mode}.xml", url)
            stops.extend(parse_hk1980_stop_xml(auxiliary, source_prefix=f"transport:{mode}"))
        return _deduplicate_source_ids(stops)

    def _load_railway_accesses(self) -> list[PoiRecord]:
        payload = self._cached_bytes("igeocom.geojson", IGEO_COM_URL)
        candidates = parse_geojson_pois(payload, source_prefix="igeocom")
        railway = [poi for poi in candidates if _looks_like_railway_access(poi)]
        if not railway:
            raise OfficialSourceError("官方 iGeoCom 资料未找到铁路站出入口坐标。")
        return railway

    def _load_hk8_main_campuses(self) -> list[PoiRecord]:
        payload = self._cached_bytes("ugc_hk8.csv", UGC_HK8_URL)
        campuses = parse_ugc_hk8_csv(payload)
        if len(campuses) < 8:
            raise OfficialSourceError("UGC 主校园资料少于八所，无法计算香港八大距离。")
        return campuses

    def _cached_bytes(self, filename: str, url: str) -> bytes:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # A current-source run deliberately starts a fresh dated cache on a
        # new Hong Kong calendar date.  Files are still reused within the same
        # run/day, so retries and SQLite resume do not redownload them.
        target = self.cache_dir / hong_kong_today().isoformat() / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file() and not self.refresh:
            return target.read_bytes()
        try:
            content = self.fetcher(url)
        except OfficialSourceError:
            raise
        except Exception as exc:
            raise OfficialSourceError(f"无法下载官方资料 {filename}。") from exc
        if not content:
            raise OfficialSourceError(f"官方资料 {filename} 为空。")
        target.write_bytes(content)
        return content

    def _download(self, url: str) -> bytes:
        request = Request(
            url,
            headers={"User-Agent": "HKPTransactionResearchBot/0.2 (public official-source downloader)"},
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                return response.read()
        except HTTPError as exc:
            raise OfficialSourceError(f"官方资料下载失败（HTTP {exc.code}）。") from exc
        except URLError as exc:
            raise OfficialSourceError("无法连接官方开放资料来源。") from exc


def parse_gtfs_stops(payload: bytes) -> list[PoiRecord]:
    """Parse Transport Department GTFS ``stops.txt`` (WGS84 coordinates)."""

    try:
        text = payload.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OfficialSourceError("公共交通 GTFS 站点资料不是 UTF-8。") from exc
    reader = csv.DictReader(io.StringIO(text))
    required = {"stop_id", "stop_lat", "stop_lon"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise OfficialSourceError("公共交通 GTFS 站点资料缺少必要坐标栏位。")
    pois: list[PoiRecord] = []
    for row in reader:
        stop_id = _text(row.get("stop_id"))
        latitude = _float(row.get("stop_lat"))
        longitude = _float(row.get("stop_lon"))
        if not stop_id or latitude is None or longitude is None:
            continue
        try:
            pois.append(
                PoiRecord(
                    source_id=f"transport:{stop_id}",
                    latitude=latitude,
                    longitude=longitude,
                    name=_text(row.get("stop_name")),
                    category="public_transport",
                )
            )
        except ValueError:
            continue
    if not pois:
        raise OfficialSourceError("公共交通 GTFS 站点资料没有可用站点。")
    return pois


def parse_hk1980_stop_xml(
    payload: bytes,
    *,
    source_prefix: str,
    coordinate_transformer: Callable[[float, float], tuple[float, float]] | None = None,
) -> list[PoiRecord]:
    """Parse a Transport Department XML stop table in HK1980 Grid coordinates.

    ``coordinate_transformer`` exists for deterministic tests.  Production
    uses :mod:`pyproj`'s EPSG:2326-to-EPSG:4326 conversion, preserving the
    official grid's accuracy before the downstream 20-metre deduplication.
    """

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as exc:
        raise OfficialSourceError("公共交通 XML 站点资料无效。") from exc

    transform = coordinate_transformer or _hk1980_to_wgs84
    pois: list[PoiRecord] = []
    for index, node in enumerate(root.findall(".//STOP"), start=1):
        stop_id = _xml_text(node, "STOP_ID") or str(index)
        easting = _float(_xml_text(node, "X"))
        northing = _float(_xml_text(node, "Y"))
        if easting is None or northing is None:
            continue
        try:
            latitude, longitude = transform(easting, northing)
            pois.append(
                PoiRecord(
                    source_id=f"{source_prefix}:{stop_id}",
                    latitude=latitude,
                    longitude=longitude,
                    category="public_transport",
                )
            )
        except (ValueError, OfficialSourceError):
            continue
    if not pois:
        raise OfficialSourceError("公共交通 XML 站点资料没有可用坐标。")
    return pois


def parse_ugc_hk8_csv(payload: bytes) -> list[PoiRecord]:
    """Parse the UGC eight-main-campus public CSV (Chinese or English headers)."""

    text = _decode_text(payload)
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise OfficialSourceError("UGC 主校园资料没有标题列。")
    selected: dict[str, PoiRecord] = {}
    for index, row in enumerate(reader, start=1):
        latitude = _row_coordinate(row, "latitude")
        longitude = _row_coordinate(row, "longitude")
        name = _row_text(row, "name")
        institution = _hk8_institution_id(name)
        if latitude is None or longitude is None or institution is None:
            continue
        try:
            selected.setdefault(
                institution,
                PoiRecord(
                    source_id=f"ugc:{institution}",
                    latitude=latitude,
                    longitude=longitude,
                    name=name,
                    category="hk8_main_campus",
                )
            )
        except ValueError:
            continue
    return list(selected.values())


def parse_school_location_workbook(payload: bytes) -> list[PoiRecord]:
    """Parse EDB school workbook with tolerant English/Chinese header mapping."""

    try:
        from openpyxl import load_workbook
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise OfficialSourceError("解析教育局学校资料需要 openpyxl。") from exc

    try:
        workbook = load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except Exception as exc:
        raise OfficialSourceError("无法读取教育局学校位置 XLSX。") from exc

    pois: list[PoiRecord] = []
    for sheet in workbook.worksheets:
        header_row, header_map = _workbook_header_map(sheet)
        if header_row is None:
            continue
        for index, row in enumerate(
            sheet.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1
        ):
            values = {header: row[position] if position < len(row) else None for header, position in header_map.items()}
            latitude = _mapping_coordinate(values, "latitude")
            longitude = _mapping_coordinate(values, "longitude")
            name = _mapping_text(values, "name")
            address = _mapping_text(values, "address")
            school_level = _mapping_text(values, "school_level")
            if latitude is None or longitude is None or not name:
                continue
            code = _mapping_text(values, "id") or _stable_id(name, index)
            try:
                pois.append(
                    PoiRecord(
                        source_id=f"school:{code}",
                        latitude=latitude,
                        longitude=longitude,
                        name=name,
                        category="formal_school",
                        physical_key=_school_institution_key(name, address, school_level),
                    )
                )
            except ValueError:
                continue
    if not pois:
        raise OfficialSourceError("教育局学校资料没有可识别的名称与坐标。")
    return _deduplicate_source_ids(pois)


def parse_geojson_pois(payload: bytes, *, source_prefix: str) -> list[PoiRecord]:
    """Parse GeoJSON point features without assuming provider-specific keys."""

    try:
        data = json.loads(_decode_text(payload))
    except json.JSONDecodeError as exc:
        raise OfficialSourceError("官方 GeoJSON 资料无效。") from exc
    features = data.get("features") if isinstance(data, dict) else None
    if not isinstance(features, list):
        raise OfficialSourceError("官方 GeoJSON 缺少 features。")

    pois: list[PoiRecord] = []
    for index, feature in enumerate(features, start=1):
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry") if isinstance(feature.get("geometry"), dict) else {}
        coordinates = geometry.get("coordinates")
        if not isinstance(coordinates, list) or len(coordinates) < 2:
            continue
        longitude = _float(coordinates[0])
        latitude = _float(coordinates[1])
        if latitude is None or longitude is None:
            continue
        properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
        name = _mapping_text(properties, "name")
        identifier = _mapping_text(properties, "id") or _stable_id(name or "poi", index)
        category = " ".join(str(value) for value in properties.values() if value is not None)
        try:
            pois.append(
                PoiRecord(
                    source_id=f"{source_prefix}:{identifier}",
                    latitude=latitude,
                    longitude=longitude,
                    name=name,
                    category=category,
                )
            )
        except ValueError:
            continue
    if not pois:
        raise OfficialSourceError("官方 GeoJSON 资料没有可用点位。")
    return _deduplicate_source_ids(pois)


def _workbook_header_map(sheet: Any) -> tuple[int | None, dict[str, int]]:
    for row_number, row in enumerate(sheet.iter_rows(min_row=1, max_row=12, values_only=True), start=1):
        mapping = {str(value).strip(): index for index, value in enumerate(row) if value not in {None, ""}}
        normalized = " ".join(mapping).casefold()
        if ("lat" in normalized or "緯度" in normalized or "纬度" in normalized) and (
            "lon" in normalized or "經度" in normalized or "经度" in normalized
        ):
            return row_number, mapping
    return None, {}


def _looks_like_railway_access(poi: PoiRecord) -> bool:
    corpus = f"{poi.name or ''} {poi.category or ''}".casefold()
    english = "railway station access" in corpus or "mtr" in corpus or "light rail" in corpus
    chinese = any(token in corpus for token in ("港鐵", "港铁", "輕鐵", "轻铁", "鐵路站", "铁路站"))
    return english or chinese


def _row_coordinate(row: dict[str, Any], dimension: str) -> float | None:
    return _mapping_coordinate(row, dimension)


def _mapping_coordinate(mapping: dict[str, Any], dimension: str) -> float | None:
    aliases = {
        "latitude": ("latitude", "lat", "緯度", "纬度"),
        "longitude": ("longitude", "lon", "lng", "經度", "经度"),
    }[dimension]
    for key, value in mapping.items():
        normalized = str(key).casefold().replace(" ", "")
        if any(alias.casefold() in normalized for alias in aliases):
            parsed = _coordinate(value)
            if parsed is not None:
                return parsed
    return None


def _row_text(row: dict[str, Any], purpose: str) -> str | None:
    return _mapping_text(row, purpose)


def _mapping_text(mapping: dict[str, Any], purpose: str) -> str | None:
    aliases = {
        "name": ("name", "schoolname", "institution", "facilities", "設施名稱", "学校名称", "學校名稱", "名稱"),
        "id": ("id", "code", "schoolcode", "學校編號", "学校编号", "編號", "编号"),
        "address": ("address", "地址"),
        "school_level": ("schoollevel", "schooltype", "學校類型", "学校类型"),
    }[purpose]
    for key, value in mapping.items():
        normalized = str(key).casefold().replace(" ", "")
        if any(alias.casefold() in normalized for alias in aliases):
            parsed = _text(value)
            if parsed:
                return parsed
    return None


def _school_institution_key(
    name: str,
    address: str | None,
    school_level: str | None,
) -> str | None:
    """Return a stable key for one school at one address and education level.

    The EDB register can represent A.M., P.M., and whole-day sessions as
    separate rows. They share this key and therefore count as one institution.
    Missing address or level data stays unmerged rather than being guessed.
    """

    if not address or not school_level:
        return None
    components = (name, address, school_level)
    normalized = [re.sub(r"\\s+", " ", value).strip().casefold() for value in components]
    return "school-institution:" + "|".join(normalized)


def _hk8_institution_id(name: str | None) -> str | None:
    if not name:
        return None
    normalized = re.sub(r"[\s\-—–,，.()（）]", "", name).casefold()
    for institution, aliases in HK8_MAIN_CAMPUS_NAMES.items():
        for alias in aliases:
            normalized_alias = re.sub(r"[\s\-—–,，.()（）]", "", alias).casefold()
            if normalized_alias and normalized_alias in normalized:
                return institution
    return None


def _coordinate(value: Any) -> float | None:
    number = _float(value)
    if number is not None:
        return number
    if not isinstance(value, str):
        return None
    numbers = [float(item) for item in re.findall(r"[-+]?\d+(?:\.\d+)?", value)]
    if not numbers:
        return None
    if len(numbers) == 1:
        return numbers[0]
    # Degrees/minutes/seconds direction strings, common in public school files.
    degrees, minutes = numbers[0], numbers[1]
    seconds = numbers[2] if len(numbers) > 2 else 0.0
    result = abs(degrees) + minutes / 60 + seconds / 3600
    return -result if any(marker in value.upper() for marker in ("S", "W")) or degrees < 0 else result


def _decode_text(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "big5", "cp950"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise OfficialSourceError("官方资料的文字编码无法识别。")


def _hk1980_to_wgs84(easting: float, northing: float) -> tuple[float, float]:
    """Transform a Hong Kong 1980 Grid coordinate to WGS84 latitude/longitude."""

    try:
        transformer = _hk1980_transformer()
        longitude, latitude = transformer.transform(easting, northing)
        return float(latitude), float(longitude)
    except Exception as exc:
        raise OfficialSourceError("无法转换运输署 HK1980 公共交通坐标。") from exc


@lru_cache(maxsize=1)
def _hk1980_transformer() -> Any:
    try:
        from pyproj import Transformer
    except ImportError as exc:  # pragma: no cover - dependency contract
        raise OfficialSourceError("转换运输署 HK1980 坐标需要 pyproj。") from exc
    return Transformer.from_crs("EPSG:2326", "EPSG:4326", always_xy=True)


def _xml_text(node: ElementTree.Element, tag: str) -> str | None:
    child = node.find(tag)
    return _text(child.text if child is not None else None)


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    return result or None


def _stable_id(value: str, index: int) -> str:
    digest = hashlib.sha1(f"{value}|{index}".encode("utf-8")).hexdigest()[:16]
    return digest


def _deduplicate_source_ids(pois: list[PoiRecord]) -> list[PoiRecord]:
    seen: set[str] = set()
    result: list[PoiRecord] = []
    for poi in pois:
        if poi.source_id not in seen:
            result.append(poi)
            seen.add(poi.source_id)
    return result

