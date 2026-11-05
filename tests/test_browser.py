from __future__ import annotations

import unittest

from hkp_transaction_crawler.browser import (
    BrowserAccessRestrictedError,
    BrowserPaginationError,
    BrowserRobotsDeniedError,
    BrowserSnapshot,
    PlaywrightPublicListNavigator,
    PublicListPaginator,
)


LIST_URL = "https://www.hkp.com.hk/zh-cn/list/transaction/example-E-E0001"


def snapshot(number: int, *, url: str = LIST_URL) -> BrowserSnapshot:
    return BrowserSnapshot(
        requested_url=url,
        final_url=url,
        html=f'<html><script id="__NEXT_DATA__">{{"page":{number}}}</script></html>',
    )


class FakeNavigator:
    def __init__(self, pages: list[BrowserSnapshot]) -> None:
        self.pages = pages
        self.index = 0
        self.closed = False

    def open(self, url: str) -> BrowserSnapshot:
        self.index = 0
        return self.pages[0]

    def next_page(self) -> BrowserSnapshot | None:
        self.index += 1
        if self.index >= len(self.pages):
            return None
        return self.pages[self.index]

    def close(self) -> None:
        self.closed = True


class FakePlaywrightPage:
    def __init__(self) -> None:
        self.url = LIST_URL
        self.goto_calls: list[tuple[str, str, int]] = []
        self.selector_calls: list[tuple[str, str, int]] = []

    def goto(self, url: str, *, wait_until: str, timeout: int) -> None:
        self.goto_calls.append((url, wait_until, timeout))

    def wait_for_selector(self, selector: str, *, state: str, timeout: int) -> None:
        self.selector_calls.append((selector, state, timeout))

    def content(self) -> str:
        return '<html><script id="__NEXT_DATA__">{}</script></html>'


class FakeClientSidePage(FakePlaywrightPage):
    def __init__(self, *, fail_advance_wait: bool = False) -> None:
        super().__init__()
        self.fail_advance_wait = fail_advance_wait
        self.function_calls: list[tuple[str, object, int]] = []

    def wait_for_function(self, script: str, *, arg: object, timeout: int) -> None:
        self.function_calls.append((script, arg, timeout))
        if self.fail_advance_wait:
            raise TimeoutError("page did not advance")


class FakeNextButton:
    def __init__(self, page: FakeClientSidePage, *, next_url: str | None = None) -> None:
        self.page = page
        self.next_url = next_url
        self.click_timeouts: list[int] = []

    def get_attribute(self, name: str) -> None:
        self.last_attribute_name = name
        return None

    def click(self, *, timeout: int) -> None:
        self.click_timeouts.append(timeout)
        if self.next_url is not None:
            self.page.url = self.next_url


class BrowserPaginatorTests(unittest.TestCase):
    def test_playwright_waits_for_hidden_next_data_to_attach(self) -> None:
        navigator = PlaywrightPublicListNavigator(timeout_seconds=7)
        page = FakePlaywrightPage()
        navigator._page = page

        result = navigator.open(LIST_URL)

        self.assertEqual(result.html, page.content())
        self.assertEqual(
            page.selector_calls,
            [
                ("script#__NEXT_DATA__", "attached", 7000),
                (navigator._PUBLIC_DETAIL_SELECTOR, "attached", 7000),
            ],
        )

    def test_client_side_next_page_accepts_a_route_change_then_waits_for_cards(self) -> None:
        navigator = PlaywrightPublicListNavigator(timeout_seconds=7)
        page = FakeClientSidePage()
        navigator._page = page
        button = FakeNextButton(page, next_url=f"{LIST_URL}?page=2")
        navigator._find_next_control = lambda: button  # type: ignore[method-assign]
        navigator._transaction_link_marker = lambda: "first page"  # type: ignore[method-assign]

        result = navigator.next_page()

        self.assertEqual(result.final_url, f"{LIST_URL}?page=2")
        self.assertEqual(button.click_timeouts, [7000])
        self.assertIn("window.location.href !== previous.url", navigator._PAGE_ADVANCEMENT_SCRIPT)
        self.assertEqual(
            page.function_calls,
            [
                (
                    navigator._PAGE_ADVANCEMENT_SCRIPT,
                    {"url": LIST_URL, "marker": "first page"},
                    7000,
                )
            ],
        )
        self.assertEqual(
            page.selector_calls,
            [
                ("script#__NEXT_DATA__", "attached", 7000),
                (navigator._PUBLIC_DETAIL_SELECTOR, "attached", 7000),
            ],
        )

    def test_client_side_next_page_raises_clear_error_only_after_wait_expires(self) -> None:
        navigator = PlaywrightPublicListNavigator(timeout_seconds=7)
        page = FakeClientSidePage(fail_advance_wait=True)
        navigator._page = page
        button = FakeNextButton(page)
        navigator._find_next_control = lambda: button  # type: ignore[method-assign]
        navigator._transaction_link_marker = lambda: "first page"  # type: ignore[method-assign]

        with self.assertRaisesRegex(BrowserPaginationError, "点击下一页后"):
            navigator.next_page()
        self.assertEqual(button.click_timeouts, [7000])

    def test_yields_visible_pages_and_respects_delay(self) -> None:
        navigator = FakeNavigator([snapshot(1), snapshot(2)])
        sleeps: list[float] = []
        paginator = PublicListPaginator(
            navigator,
            robots_allowed=lambda _: True,
            delay_seconds=3,
            sleeper=sleeps.append,
        )

        pages = list(paginator.iter_pages(LIST_URL))

        self.assertEqual([page.page_number for page in pages], [1, 2])
        self.assertEqual(sleeps, [3, 3])
        self.assertTrue(navigator.closed)

    def test_stops_when_robots_denies_initial_page(self) -> None:
        navigator = FakeNavigator([snapshot(1)])
        paginator = PublicListPaginator(navigator, robots_allowed=lambda _: False)

        with self.assertRaises(BrowserRobotsDeniedError):
            list(paginator.iter_pages(LIST_URL))
        self.assertFalse(navigator.closed)

    def test_rejects_repeated_page_content(self) -> None:
        repeated = snapshot(1)
        navigator = FakeNavigator([repeated, repeated])
        paginator = PublicListPaginator(navigator, robots_allowed=lambda _: True, delay_seconds=0)

        with self.assertRaisesRegex(BrowserPaginationError, "重复"):
            list(paginator.iter_pages(LIST_URL))
        self.assertTrue(navigator.closed)

    def test_detects_access_barrier(self) -> None:
        blocked = BrowserSnapshot(
            requested_url=LIST_URL,
            final_url=LIST_URL,
            html='<script id="__NEXT_DATA__">{}</script>captcha',
        )
        navigator = FakeNavigator([blocked])
        paginator = PublicListPaginator(navigator, robots_allowed=lambda _: True)

        with self.assertRaises(BrowserAccessRestrictedError):
            list(paginator.iter_pages(LIST_URL))
        self.assertTrue(navigator.closed)

