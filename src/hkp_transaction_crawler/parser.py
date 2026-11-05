"""Parsing of public HKP pages rendered with Next.js.

The parser reads data embedded in the page delivered to a normal browser.  It
does not call undocumented APIs or attempt to bypass access controls.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from math import log
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

from .models import FEATURE_COLUMNS, TransactionRecord
from .time_utils import HONG_KONG_TIMEZONE


class ParseError(ValueError):
    """The supplied page did not contain the expected public data."""


class _NextDataScriptParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside_target = False
        self._done = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "script" or self._done:
            return
        attributes = {key.lower(): value for key, value in attrs}
        if attributes.get("id") == "__NEXT_DATA__":
            self._inside_target = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside_target:
            self._inside_target = False
            self._done = True

    def handle_data(self, data: str) -> None:
        if self._inside_target:
            self.parts.append(data)


class _DetailLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self.hrefs.append(href)


@dataclass(frozen=True)
class ParsedPage:
    kind: str
    records: list[TransactionRecord]
    detail_urls: list[str]


def extract_next_data(page_html: str) -> dict[str, Any]:
    """Return the JSON in the public ``__NEXT_DATA__`` script element."""

    parser = _NextDataScriptParser()
    parser.feed(page_html)
    parser.close()
    raw = "".join(parser.parts).strip()
    if not raw:
        raise ParseError("找不到页面内嵌的 __NEXT_DATA__ 数据。页面可能已经改版或受访问限制。")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ParseError("__NEXT_DATA__ 不是有效 JSON，页面可能未完整返回。") from exc
    if not isinstance(decoded, dict):
        raise ParseError("__NEXT_DATA__ 的顶层不是对象。")
    return decoded


def parse_page(page_html: str, source_url: str, scraped_at: str) -> ParsedPage:
    """Classify a detail or list page and normalize its visible records."""

    next_data = extract_next_data(page_html)
    page_props = _page_props(next_data)

    result = page_props.get("result")
    if isinstance(result, dict) and isinstance(result.get("transactionData"), dict):
        record = transaction_from_json(result["transactionData"], source_url, scraped_at)
        return ParsedPage(kind="detail", records=[record], detail_urls=[source_url])

    listing = page_props.get("listingDataForSSR")
    if isinstance(listing, dict):
        raw_records = listing.get("result")
        records: list[TransactionRecord] = []
        detail_urls: list[str] = []
        if isinstance(raw_records, list):
            for item in raw_records:
                if not isinstance(item, dict):
                    continue
                detail_url = _as_text(item.get("url_desc")) or source_url
                try:
                    records.append(transaction_from_json(item, detail_url, scraped_at))
                except ParseError:
                    continue
                if detail_url != source_url:
                    detail_urls.append(detail_url)
        # Some public list responses omit SSR records.  Visible detail links
        # remain useful, but the caller decides whether to fetch them.
        detail_urls.extend(extract_detail_urls(page_html, source_url))
        return ParsedPage(
            kind="list",
            records=records,
            detail_urls=_deduplicate(detail_urls),
        )

    raise ParseError("这不是可识别的香港置业成交详情页或成交列表页。")


def extract_detail_urls(page_html: str, page_url: str) -> list[str]:
    """Find public transaction-detail links rendered in a list page."""

    parser = _DetailLinkParser()
    parser.feed(page_html)
    parser.close()

    page_host = urlparse(page_url).netloc.lower()
    urls: list[str] = []
    for href in parser.hrefs:
        absolute = urljoin(page_url, href)
        parsed = urlparse(absolute)
        if (
            parsed.scheme == "https"
            and parsed.netloc.lower() == page_host
            and _is_public_transaction_detail_path(parsed.path)
        ):
            urls.append(absolute)
    return _deduplicate(urls)


def _is_public_transaction_detail_path(path: str) -> bool:
    """Return whether a URL path is a single public HKP transaction page."""

    parts = [part for part in unquote(path).split("/") if part]
    return len(parts) == 3 and parts[1].casefold() == "transaction" and bool(parts[2])


def transaction_from_json(payload: dict[str, Any], source_url: str, scraped_at: str) -> TransactionRecord:
    """Normalize a public HKP payload into the agreed A:AA model schema."""

    record_id = _as_text(payload.get("id"))
    if not record_id:
        raise ParseError("成交记录缺少 id，已跳过。")

    estate = _as_dict(payload.get("estate"))
    phase = _as_dict(payload.get("phase"))
    building = _as_dict(payload.get("building"))
    region = _as_dict(payload.get("region"))
    subregion = _as_dict(payload.get("subregion"))
    district = _as_dict(payload.get("district"))
    floor_level = _as_dict(payload.get("floor_level"))
    location = _as_dict(payload.get("location"))

    transaction_code = _as_text(payload.get("tx_type"))
    features = _as_list(payload.get("feature"))
    feature_flags = _features(features)

    latitude, longitude, coordinate_precision = _coordinates(building, location, estate)
    transaction_timestamp = _as_text(payload.get("tx_date"))
    transaction_date = _to_hong_kong_date(transaction_timestamp)
    completion_year = _year_from_date(_as_text(estate.get("first_op_date")))
    transaction_year = _year_from_date(transaction_date)
    property_age = (
        transaction_year - completion_year
        if transaction_year is not None and completion_year is not None
        else None
    )
    price_hkd = _number(payload.get("price"))
    saleable_area_sqft = _number(payload.get("net_area"))
    building_type_raw = _as_text(building.get("bldg_type"))

    return TransactionRecord(
        record_id=record_id,
        source_url=source_url,
        transaction_date=transaction_date,
        price_hkd=price_hkd,
        log_price=_positive_log(price_hkd),
        transaction_month_index=_month_index(transaction_date),
        # User selected the broader HKP subregion rather than its more granular
        # district name as the categorical regression variable.
        district=_as_text(subregion.get("name")),
        saleable_area_sqft=saleable_area_sqft,
        log_area=_positive_log(saleable_area_sqft),
        bedrooms=_bedrooms(payload.get("bedroom")),
        completion_year=completion_year,
        property_age=property_age,
        property_age_sq=property_age**2 if property_age is not None else None,
        floor=_floor_value(payload.get("floor")),
        floor_band=_floor_band(floor_level),
        property_type=_property_type(building_type_raw),
        latitude=latitude,
        longitude=longitude,
        coordinate_precision=coordinate_precision,
        geo_status="pending" if latitude is not None and longitude is not None else "missing_coordinate",
        scraped_at=scraped_at,
        tx_type=transaction_code,
        mkt_type=_as_text(payload.get("mkt_type")),
        region=_as_text(region.get("name")),
        hk_detail_district=_as_text(district.get("name")),
        estate_id=_as_text(estate.get("id")),
        phase_id=_as_text(phase.get("id")),
        building_id=_as_text(building.get("id")),
        building_type_raw=building_type_raw,
        raw_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        **feature_flags,
    )


def _page_props(next_data: dict[str, Any]) -> dict[str, Any]:
    props = _as_dict(next_data.get("props"))
    page_props = _as_dict(props.get("pageProps"))
    if not page_props:
        raise ParseError("页面没有 props.pageProps，无法读取成交资料。")
    return page_props


def _features(features: list[Any]) -> dict[str, int]:
    ids: list[str] = []
    for item in features:
        if isinstance(item, dict):
            feature_id = _as_text(item.get("id"))
        else:
            feature_id = _as_text(item)
        if feature_id:
            ids.append(feature_id)

    present = set(ids)
    return {column: int(feature_id in present) for feature_id, column in FEATURE_COLUMNS.items()}


def _coordinates(
    building: dict[str, Any],
    location: dict[str, Any],
    estate: dict[str, Any],
) -> tuple[float | None, float | None, str]:
    building_latitude = _number(building.get("latitude"))
    building_longitude = _number(building.get("longitude"))
    if building_latitude is not None and building_longitude is not None:
        return building_latitude, building_longitude, "building"
    listing_latitude = _number(location.get("lat"))
    listing_longitude = _number(location.get("lon"))
    if listing_latitude is not None and listing_longitude is not None:
        return listing_latitude, listing_longitude, "listing"
    estate_latitude = _number(estate.get("latitude"))
    estate_longitude = _number(estate.get("longitude"))
    if estate_latitude is not None and estate_longitude is not None:
        return estate_latitude, estate_longitude, "estate"
    return None, None, "missing"


def _month_index(transaction_date: str | None) -> int | None:
    if not transaction_date:
        return None
    try:
        parsed = datetime.fromisoformat(transaction_date)
    except ValueError:
        return None
    # The user's original workbook explicitly defines 2020-01 as 1.
    return (parsed.year - 2020) * 12 + parsed.month


def _year_from_date(value: str | None) -> int | None:
    if not value or len(value) < 4:
        return None
    try:
        return int(value[:4])
    except ValueError:
        return None


def _positive_log(value: float | None) -> float | None:
    return log(value) if value is not None and value > 0 else None


def _bedrooms(value: Any) -> int | None:
    result = _integer(value)
    if result is None or result < 0:
        return None
    return min(result, 5)


def _floor_value(value: Any) -> str:
    text = _as_text(value)
    if not text or text.upper() in {"-", "--", "N/A", "NA", "NONE", "NULL", "UNKNOWN"}:
        return "NA"
    return text


def _floor_band(floor_level: dict[str, Any]) -> str:
    code = (_as_text(floor_level.get("id")) or "").upper()
    name = _as_text(floor_level.get("name")) or ""
    if code == "L" or "低" in name or "LOW" in name.upper():
        return "low"
    if code == "M" or "中" in name or "MID" in name.upper():
        return "mid"
    if code == "H" or "高" in name or "HIGH" in name.upper():
        return "high"
    return "unknown"


def _property_type(value: str | None) -> str:
    normalized = (value or "").strip().upper().replace("-", "_").replace(" ", "_")
    if not normalized:
        return "unknown"
    if normalized in {"APARTMENT", "FLAT", "TOWER", "RESIDENTIAL"}:
        return "tower"
    if normalized in {"HOUSE", "VILLA", "VILLAGE_HOUSE", "TOWNHOUSE", "DETACHED_HOUSE"}:
        return "house"
    return "other"


def _to_hong_kong_date(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:10] if len(value) >= 10 else value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(HONG_KONG_TIMEZONE).date().isoformat()


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _join_values(values: list[Any]) -> str | None:
    normalized = [_as_text(value) for value in values]
    clean = [value for value in normalized if value]
    return " | ".join(clean) if clean else None


def _deduplicate(urls: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for url in urls:
        if url not in seen:
            result.append(url)
            seen.add(url)
    return result

