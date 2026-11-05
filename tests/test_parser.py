from __future__ import annotations

import json
import unittest

from hkp_transaction_crawler.parser import ParseError, parse_page


DETAIL_URL = "https://www.hkp.com.hk/zh-cn/transaction/example-I123"
LIST_URL = "https://www.hkp.com.hk/zh-cn/list/transaction/example-E-E1"


def page_with_data(page_props: dict, extra_html: str = "") -> str:
    data = {"props": {"pageProps": page_props}}
    return (
        "<html><head></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(data, ensure_ascii=False)}</script>'
        f"{extra_html}</body></html>"
    )


class ParserTests(unittest.TestCase):
    def test_detail_maps_transaction_and_feature_flags(self) -> None:
        payload = {
            "id": "I123",
            "tx_date": "2026-10-30T16:00:00.000Z",
            "tx_type": "S",
            "mkt_type": "2",
            "transaction_type": "NORM",
            "source": "LANDREG",
            "original_source": "MIDLAND",
            "tags": ["LANDREG"],
            "region": {"name": "九龙"},
            "subregion": {"name": "将军澳"},
            "district": {"name": "将军澳市中心"},
            "estate": {
                "id": "E1",
                "name": "天晋",
                "estate_page_name": "天晋 1期",
                "latitude": 22.30,
                "longitude": 114.26,
                "first_op_date": "2011-08-30T16:00:00.000Z",
            },
            "phase": {"id": "P1", "name": "1期"},
            "building": {
                "id": "B1",
                "name": "2座",
                "address": "唐贤街9号",
                "latitude": 22.31,
                "longitude": 114.27,
                "bldg_type": "APARTMENT",
            },
            "floor_level": {"name": "低层"},
            "floor": "16",
            "flat": "D",
            "feature": [
                {"id": "balcony", "name": "连阳台"},
                {"id": "utility_platform", "name": "连生活阳台"},
                {"id": "roof", "name": "连天台"},
            ],
            "bedroom": 2,
            "sitting_room": 2,
            "orientation": "西北",
            "net_area": 536,
            "area": 693,
            "price": 10400000,
            "unit_price_net": 19403,
            "history_tx": [
                {"tx_date": "2023-09-10T16:00:00.000Z", "price": 9638000, "gain": "7.91"}
            ],
        }
        html = page_with_data({"result": {"transactionData": payload}})

        parsed = parse_page(html, DETAIL_URL, "2026-10-31T00:00:00+00:00")

        self.assertEqual(parsed.kind, "detail")
        self.assertEqual(len(parsed.records), 1)
        record = parsed.records[0]
        self.assertEqual(record.record_id, "I123")
        self.assertEqual(record.transaction_date, "2026-10-31")
        self.assertEqual(record.tx_type, "S")
        self.assertEqual(record.mkt_type, "2")
        self.assertEqual(record.district, "将军澳")
        self.assertEqual(record.region, "九龙")
        self.assertEqual(record.price_hkd, 10_400_000.0)
        self.assertEqual(record.saleable_area_sqft, 536.0)
        self.assertAlmostEqual(record.log_price or 0, 16.1573, places=3)
        self.assertEqual(record.transaction_month_index, 82)
        self.assertEqual(record.completion_year, 2011)
        self.assertEqual(record.property_age, 15)
        self.assertEqual(record.property_age_sq, 225)
        self.assertEqual(record.floor, "16")
        self.assertEqual(record.floor_band, "low")
        self.assertEqual(record.property_type, "tower")
        self.assertEqual(record.has_balcony, 1)
        self.assertEqual(record.has_living_balcony, 1)
        self.assertEqual(record.has_rooftop, 1)
        self.assertEqual(record.has_terrace, 0)
        self.assertEqual(record.latitude, 22.31)
        self.assertEqual(record.longitude, 114.27)
        self.assertEqual(record.coordinate_precision, "building")
        self.assertEqual(record.geo_status, "pending")

    def test_list_maps_visible_records_and_detail_urls(self) -> None:
        list_record = {
            "id": "I456",
            "tx_date": "2026-10-30T16:00:00.000Z",
            "tx_type": "L",
            "estate": {"id": "E1", "name": "天晋"},
            "phase": {"id": "P1", "name": "1期"},
            "building": {"id": "B1", "name": "2座"},
            "flat": "D",
            "net_area": 536,
            "price": 26500,
            "unit_price_net": 49,
            "feature": [{"id": "flat_roof", "name": "平台"}],
            "location": {"lat": 22.31, "lon": 114.27},
            "url_desc": DETAIL_URL,
        }
        html = page_with_data(
            {"listingDataForSSR": {"result": [list_record]}},
            f'<a href="{DETAIL_URL}">详情</a>',
        )

        parsed = parse_page(html, LIST_URL, "2026-10-31T00:00:00+00:00")

        self.assertEqual(parsed.kind, "list")
        self.assertEqual(len(parsed.records), 1)
        self.assertEqual(parsed.records[0].tx_type, "L")
        self.assertEqual(parsed.records[0].floor, "NA")
        self.assertEqual(parsed.records[0].has_terrace, 1)
        self.assertEqual(parsed.detail_urls, [DETAIL_URL])

    def test_ignores_map_transaction_links_when_extracting_detail_urls(self) -> None:
        map_url = "https://www.hkp.com.hk/zh-cn/map/transaction/example-E-E1"
        html = page_with_data(
            {"listingDataForSSR": {"result": []}},
            f'<a href="{map_url}">地图</a><a href="{DETAIL_URL}">详情</a>',
        )

        parsed = parse_page(html, LIST_URL, "2026-10-31T00:00:00+00:00")

        self.assertEqual(parsed.detail_urls, [DETAIL_URL])

    def test_invalid_page_raises_clear_error(self) -> None:
        with self.assertRaises(ParseError):
            parse_page("<html></html>", DETAIL_URL, "2026-10-31T00:00:00+00:00")

