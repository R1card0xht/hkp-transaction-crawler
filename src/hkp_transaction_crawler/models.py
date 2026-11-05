"""Final regression-schema record and shared column definitions.

The crawler retains a small amount of non-exported source context for refresh,
filtering, and geospatial enrichment. CSV/XLSX files always follow the exact
A:AA contract in ``EXPORT_COLUMNS``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


# HKP's public feature IDs are stable across Chinese locale variants.  IDs,
# rather than display labels, protect the six binary columns from translation
# differences between page versions.
FEATURE_COLUMNS = {
    "balcony": "has_balcony",
    "utility_platform": "has_living_balcony",
    "flat_roof": "has_terrace",
    "carpark": "has_parking",
    "roof": "has_rooftop",
    "garden": "has_garden",
}


# Exact A:AB output contract. A:E are identifiers/target; F:AB are the 23
# agreed explanatory factors.
EXPORT_COLUMNS = [
    "record_id",
    "source_url",
    "transaction_date",
    "price_hkd",
    "log_price",
    "transaction_month_index",
    "district",
    "saleable_area_sqft",
    "log_area",
    "bedrooms",
    "completion_year",
    "property_age",
    "property_age_sq",
    "floor",
    "floor_band",
    "property_type",
    "has_balcony",
    "has_living_balcony",
    "has_terrace",
    "has_parking",
    "has_rooftop",
    "has_garden",
    "mtr_walk_min",
    "mall_walk_min",
    "school_count_within_1km",
    "nearest_hk8_university_distance_km",
    "medical_walk_min",
    "district_price_index",
]


DISPLAY_LABELS = {
    "record_id": "记录编号",
    "source_url": "原始网页链接",
    "transaction_date": "成交日期",
    "price_hkd": "成交总价（HKD）",
    "log_price": "LN(成交总价)：目标变量 y",
    "transaction_month_index": "成交月份序号（2020-01 = 1）",
    "district": "地区（较大分区）",
    "saleable_area_sqft": "实用面积（呎）",
    "log_area": "LN(实用面积）",
    "bedrooms": "房间数：开放式=0，5房+=5",
    "completion_year": "入伙年份",
    "property_age": "成交时楼龄",
    "property_age_sq": "楼龄平方",
    "floor": "具体楼层（无数据=NA）",
    "floor_band": "楼层分组：low / mid / high / unknown",
    "property_type": "物业类型：tower / house / other / unknown",
    "has_balcony": "连阳台：0/1",
    "has_living_balcony": "连生活阳台：0/1",
    "has_terrace": "连露台（平台）：0/1",
    "has_parking": "连车位：0/1",
    "has_rooftop": "连天台：0/1",
    "has_garden": "连花园：0/1",
    "mtr_walk_min": "最近港铁／轻铁步行分钟",
    "mall_walk_min": "最近正式购物商场步行分钟",
    "school_count_within_1km": "1 km 内正式学校数量",
    "nearest_hk8_university_distance_km": "最近香港八大主校园步行距离（km）",
    "medical_walk_min": "最近医院／政府诊所／综合医疗中心步行分钟",
    "district_price_index": "官方区域月度楼价指数",
}


# Retained in SQLite and the geographic cache for backward-compatible resume,
# but intentionally omitted from the final CSV/XLSX factor schema.
PERSISTENCE_ONLY_COLUMNS = [
    "public_transport_stops_within_1km",
]


# The following values are intentionally not included in model export.
INTERNAL_COLUMNS = [
    "scraped_at",
    "tx_type",
    "mkt_type",
    "region",
    "hk_detail_district",
    "estate_id",
    "phase_id",
    "building_id",
    "building_type_raw",
    "latitude",
    "longitude",
    "coordinate_precision",
    "geo_snapshot_date",
    "geo_status",
    "raw_json",
]


@dataclass(slots=True)
class TransactionRecord:
    """One final, model-ready secondary residential transaction record."""

    # A:AA public export fields.
    record_id: str
    source_url: str
    transaction_date: str | None = None
    price_hkd: float | None = None
    log_price: float | None = None
    transaction_month_index: int | None = None
    district: str | None = None
    saleable_area_sqft: float | None = None
    log_area: float | None = None
    bedrooms: int | None = None
    completion_year: int | None = None
    property_age: int | None = None
    property_age_sq: int | None = None
    floor: str = "NA"
    floor_band: str | None = None
    property_type: str | None = None
    has_balcony: int = 0
    has_living_balcony: int = 0
    has_terrace: int = 0
    has_parking: int = 0
    has_rooftop: int = 0
    has_garden: int = 0
    mtr_walk_min: float | None = None
    public_transport_stops_within_1km: int | None = None
    mall_walk_min: float | None = None
    school_count_within_1km: int | None = None
    nearest_hk8_university_distance_km: float | None = None
    medical_walk_min: float | None = None
    district_price_index: float | None = None

    # Internal persistence/context fields.
    scraped_at: str | None = None
    tx_type: str | None = None
    mkt_type: str | None = None
    region: str | None = None
    hk_detail_district: str | None = None
    estate_id: str | None = None
    phase_id: str | None = None
    building_id: str | None = None
    building_type_raw: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    coordinate_precision: str | None = None
    geo_snapshot_date: str | None = None
    geo_status: str | None = None
    raw_json: str | None = None

    @property
    def transaction_id(self) -> str:
        """Backward-compatible alias used by de-duplication code."""

        return self.record_id

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

