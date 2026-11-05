from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from hkp_transaction_crawler.browser import BrowserSnapshot
from hkp_transaction_crawler.client import FetchResult
from hkp_transaction_crawler.crawler import _record_id_from_detail_url, crawl_tasks
from hkp_transaction_crawler.models import TransactionRecord
from hkp_transaction_crawler.storage import TransactionStore
from hkp_transaction_crawler.tasks import CollectionTask


LIST_URL = "https://www.hkp.com.hk/zh-cn/list/transaction/example-E-E1"
DETAIL_URL = "https://www.hkp.com.hk/zh-cn/transaction/example-I123"
NO_DETAIL_URL = "https://www.hkp.com.hk/zh-cn/transaction/example-NO2026102826102800160914"
NUMERIC_DETAIL_URL = "https://www.hkp.com.hk/zh-cn/transaction/example-327702"


def page(page_props: dict) -> str:
    payload = {"props": {"pageProps": page_props}}
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(payload, ensure_ascii=False)
        + f'</script><a href="{DETAIL_URL}">详情</a></html>'
    )


def detail_html(*, tx_type: str = "S", mkt_type: str = "2") -> str:
    payload = {
        "id": "I123",
        "tx_date": "2026-10-30T16:00:00.000Z",
        "tx_type": tx_type,
        "mkt_type": mkt_type,
        "region": {"name": "九龙"},
        "subregion": {"name": "将军澳"},
        "estate": {"id": "E1", "first_op_date": "2011-01-01T00:00:00.000Z"},
        "building": {"id": "B1", "bldg_type": "APARTMENT", "latitude": 22.3, "longitude": 114.2},
        "floor_level": {"id": "M"},
        "bedroom": 2,
        "net_area": 500,
        "price": 10_000_000,
    }
    return page({"result": {"transactionData": payload}})


class _Navigator:
    def __init__(self, html: str) -> None:
        self.html = html

    def open(self, url: str) -> BrowserSnapshot:
        return BrowserSnapshot(url, url, self.html)

    def next_page(self):
        return None

    def close(self) -> None:
        pass


class _Client:
    delay_seconds = 0

    def __init__(self, html: str) -> None:
        self.html = html
        self.fetched: list[str] = []

    def allowed_by_robots(self, url: str) -> bool:
        return True

    def fetch_page(self, url: str) -> FetchResult:
        self.fetched.append(url)
        return FetchResult(url, url, 200, self.html)


class CrawlerTests(unittest.TestCase):
    def test_collects_eligible_detail_then_resumes_without_refetch(self) -> None:
        list_html = page(
            {
                "listingDataForSSR": {
                    "result": [
                        {"id": "I123", "url_desc": DETAIL_URL, "tx_type": "S", "mkt_type": "2"}
                    ]
                }
            }
        )
        client = _Client(detail_html())
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            with TransactionStore(root / "state.sqlite") as store:
                first = crawl_tasks(
                    [CollectionTask(LIST_URL, 2)],
                    client=client,
                    store=store,
                    source_cache=root / "sources",
                    navigator_factory=lambda: _Navigator(list_html),
                    end_date=date(2026, 11, 1),
                )
                second = crawl_tasks(
                    [CollectionTask(LIST_URL, 2)],
                    client=client,
                    store=store,
                    source_cache=root / "sources",
                    navigator_factory=lambda: _Navigator(list_html),
                    end_date=date(2026, 11, 1),
                )

                self.assertEqual(store.count(), 1)
            self.assertEqual(first.records_upserted, 1)
            self.assertEqual(second.records_upserted, 0)
            self.assertEqual(second.records_skipped_existing, 1)
            self.assertEqual(client.fetched, [DETAIL_URL])

    def test_proven_rental_is_filtered_before_detail_fetch(self) -> None:
        list_html = page(
            {
                "listingDataForSSR": {
                    "result": [
                        {"id": "I123", "url_desc": DETAIL_URL, "tx_type": "L", "mkt_type": "2"}
                    ]
                }
            }
        )
        client = _Client(detail_html(tx_type="L"))
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            with TransactionStore(root / "state.sqlite") as store:
                summary = crawl_tasks(
                    [CollectionTask(LIST_URL, 2)],
                    client=client,
                    store=store,
                    source_cache=root / "sources",
                    navigator_factory=lambda: _Navigator(list_html),
                    end_date=date(2026, 11, 1),
                )
                self.assertEqual(store.count(), 0)
        self.assertEqual(summary.records_skipped_not_sale, 1)
        self.assertEqual(client.fetched, [])

    def test_rejects_map_transaction_url_without_fetching_it(self) -> None:
        map_url = "https://www.hkp.com.hk/zh-cn/map/transaction/example-E-E1"
        list_html = page(
            {
                "listingDataForSSR": {
                    "result": [
                        {"id": "I123", "url_desc": map_url, "tx_type": "S", "mkt_type": "2"}
                    ]
                }
            }
        ).replace(f'<a href="{DETAIL_URL}">详情</a>', "")
        client = _Client(detail_html())
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            with TransactionStore(root / "state.sqlite") as store:
                summary = crawl_tasks(
                    [CollectionTask(LIST_URL, 2)],
                    client=client,
                    store=store,
                    source_cache=root / "sources",
                    navigator_factory=lambda: _Navigator(list_html),
                    end_date=date(2026, 11, 1),
                )
                self.assertEqual(store.count(), 0)

        self.assertEqual(summary.detail_urls_rejected, 1)
        self.assertEqual(client.fetched, [])

    def test_resumed_crawl_skips_existing_no_and_numeric_detail_urls_before_fetching(self) -> None:
        list_html = page({"listingDataForSSR": {"result": []}}).replace(
            DETAIL_URL,
            NO_DETAIL_URL,
        ).replace(
            "</html>",
            f'<a href="{NUMERIC_DETAIL_URL}">详情</a></html>',
        )
        client = _Client(detail_html())
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            with TransactionStore(root / "state.sqlite") as store:
                store.upsert_many(
                    [
                        TransactionRecord(record_id="NO2026102826102800160914", source_url=NO_DETAIL_URL),
                        TransactionRecord(record_id="327702", source_url=NUMERIC_DETAIL_URL),
                    ]
                )
                summary = crawl_tasks(
                    [CollectionTask(LIST_URL, 2)],
                    client=client,
                    store=store,
                    source_cache=root / "sources",
                    navigator_factory=lambda: _Navigator(list_html),
                    end_date=date(2026, 11, 1),
                )

        self.assertEqual(summary.records_skipped_existing, 2)
        self.assertEqual(client.fetched, [])

    def test_record_id_from_detail_url_recognizes_no_suffix(self) -> None:
        self.assertEqual(
            _record_id_from_detail_url(NO_DETAIL_URL),
            "NO2026102826102800160914",
        )

    def test_stops_at_max_records_and_resumes_from_next_record(self) -> None:
        detail_2 = "https://www.hkp.com.hk/zh-cn/transaction/example-I124"
        list_html = (
            page(
                {
                    "listingDataForSSR": {
                        "result": [
                            {"id": "I123", "url_desc": DETAIL_URL, "tx_type": "S", "mkt_type": "2"},
                            {"id": "I124", "url_desc": detail_2, "tx_type": "S", "mkt_type": "2"},
                        ]
                    }
                }
            ).replace(
                "</html>",
                f'<a href="{detail_2}">详情2</a></html>',
            )
        )

        class _MultiClient(_Client):
            def fetch_page(self, url: str) -> FetchResult:
                self.fetched.append(url)
                rid = "I124" if url.endswith("I124") else "I123"
                html = detail_html().replace('"id": "I123"', f'"id": "{rid}"')
                return FetchResult(url, url, 200, html)

        client = _MultiClient("")
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            with TransactionStore(root / "state.sqlite") as store:
                first = crawl_tasks(
                    [CollectionTask(LIST_URL, 2)],
                    client=client,
                    store=store,
                    source_cache=root / "sources",
                    max_records=1,
                    navigator_factory=lambda: _Navigator(list_html),
                    end_date=date(2026, 11, 1),
                )
                self.assertEqual(first.records_upserted, 1)
                self.assertTrue(first.stopped_at_record_limit)
                self.assertEqual(store.count(), 1)

                second = crawl_tasks(
                    [CollectionTask(LIST_URL, 2)],
                    client=client,
                    store=store,
                    source_cache=root / "sources",
                    max_records=1,
                    navigator_factory=lambda: _Navigator(list_html),
                    end_date=date(2026, 11, 1),
                )
                self.assertEqual(second.records_upserted, 1)
                self.assertEqual(second.records_skipped_existing, 1)
                self.assertEqual(store.count(), 2)
        self.assertEqual(client.fetched, [DETAIL_URL, detail_2])

