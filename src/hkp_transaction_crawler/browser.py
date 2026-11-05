"""Public-page browser pagination without direct calls to hidden HKP APIs.

The concrete Playwright driver uses only normal public page navigation and the
visible Next-page controls. It deliberately contains no endpoint, token, or
request logic for HKP's internal data services.
"""

from __future__ import annotations

import importlib
import time
from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Iterator, Protocol
from urllib.parse import urljoin

from .tasks import TaskFileError, normalize_hkp_transaction_list_url


class BrowserPaginationError(RuntimeError):
    """Base class for browser pagination failures that should stop collection."""


class BrowserUnavailableError(BrowserPaginationError):
    """Playwright or its browser binary is unavailable locally."""


class BrowserAccessRestrictedError(BrowserPaginationError):
    """The public page displayed a login, CAPTCHA, or other access barrier."""


class BrowserRobotsDeniedError(BrowserPaginationError):
    """The supplied robots checker did not allow the page URL."""


@dataclass(frozen=True)
class BrowserSnapshot:
    """A rendered public list page captured from the browser DOM."""

    requested_url: str
    final_url: str
    html: str

    @property
    def fingerprint(self) -> str:
        return sha256(self.html.encode("utf-8", errors="replace")).hexdigest()


@dataclass(frozen=True)
class BrowserListPage:
    """One unique page yielded by a public transaction-list pagination run."""

    page_number: int
    requested_url: str
    final_url: str
    html: str


class ListPageNavigator(Protocol):
    """Minimal browser adapter used by ``PublicListPaginator`` and its tests."""

    def open(self, url: str) -> BrowserSnapshot: ...

    def next_page(self) -> BrowserSnapshot | None: ...

    def close(self) -> None: ...


class PublicListPaginator:
    """Guarded iterator over visible public pages from one list URL."""

    def __init__(
        self,
        navigator: ListPageNavigator,
        *,
        robots_allowed: Callable[[str], bool],
        delay_seconds: float = 3.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        self.navigator = navigator
        self.robots_allowed = robots_allowed
        self.delay_seconds = delay_seconds
        self.sleeper = sleeper

    def iter_pages(self, start_url: str, *, max_pages: int | None = None) -> Iterator[BrowserListPage]:
        """Yield each unique rendered page until visible pagination ends.

        ``max_pages`` is a safety/testing ceiling. ``None`` follows every
        visible page. Repeated DOM content is treated as a failure rather than
        looping or silently duplicating records.
        """

        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages 必须至少为 1，或使用 None 表示不设上限。")
        try:
            normalized_url = normalize_hkp_transaction_list_url(start_url)
        except TaskFileError as exc:
            raise BrowserPaginationError(str(exc)) from exc
        self._require_robots_permission(normalized_url)

        seen_fingerprints: set[str] = set()
        page_number = 1
        try:
            snapshot = self.navigator.open(normalized_url)
            while True:
                self._validate_rendered_snapshot(snapshot)
                if snapshot.fingerprint in seen_fingerprints:
                    raise BrowserPaginationError("分页页面内容重复，程序已停止以避免重复采集。")
                seen_fingerprints.add(snapshot.fingerprint)
                yield BrowserListPage(
                    page_number=page_number,
                    requested_url=snapshot.requested_url,
                    final_url=snapshot.final_url,
                    html=snapshot.html,
                )

                if max_pages is not None and page_number >= max_pages:
                    return
                if self.delay_seconds:
                    self.sleeper(self.delay_seconds)
                next_snapshot = self.navigator.next_page()
                if next_snapshot is None:
                    return
                self._require_robots_permission(next_snapshot.final_url)
                snapshot = next_snapshot
                page_number += 1
        finally:
            self.navigator.close()

    def _require_robots_permission(self, url: str) -> None:
        if not self.robots_allowed(url):
            raise BrowserRobotsDeniedError(f"robots.txt 不允许抓取：{url}")

    @staticmethod
    def _validate_rendered_snapshot(snapshot: BrowserSnapshot) -> None:
        try:
            normalize_hkp_transaction_list_url(snapshot.final_url)
        except TaskFileError as exc:
            raise BrowserPaginationError(f"浏览器离开了允许的公开成交列表页：{snapshot.final_url}") from exc

        page = snapshot.html.casefold()
        markers = (
            "captcha",
            "verify you are human",
            "access denied",
            "unusual traffic",
            "cf-chl-",
            "sign in",
            "登录",
        )
        if any(marker in page for marker in markers):
            raise BrowserAccessRestrictedError("页面显示登录、验证码或访问限制；程序已停止。")
        if "__next_data__" not in page:
            raise BrowserPaginationError("公开列表页没有 __NEXT_DATA__，页面可能未完整加载。")


class PlaywrightPublicListNavigator:
    """Optional Playwright implementation of visible public list pagination.

    It only runs when called. Importing this package or executing offline tests
    never requires Playwright or a browser binary.
    """

    _NEXT_SELECTORS = (
        "a[rel='next']",
        "[aria-label='下一页']",
        "[aria-label='Next']",
        "[aria-label='Next page']",
        "button:has-text('下一页')",
        "button:has-text('Next')",
    )
    _PUBLIC_DETAIL_SELECTOR = (
        "a[href*='/transaction/']:not([href*='/map/transaction/'])"
        ":not([href*='/list/transaction'])"
    )
    _PAGE_ADVANCEMENT_SCRIPT = """previous => {
        const marker = [...document.querySelectorAll('a[href]')]
            .map(anchor => {
                try {
                    const url = new URL(anchor.href, document.baseURI);
                    const parts = decodeURIComponent(url.pathname)
                        .split('/')
                        .filter(Boolean);
                    return parts.length === 3 && parts[1].toLowerCase() === 'transaction'
                        ? url.href
                        : '';
                } catch (_) {
                    return '';
                }
            })
            .filter(Boolean)
            .sort()
            .join('\\n');
        return window.location.href !== previous.url
            || (marker.length > 0 && marker !== previous.marker);
    }"""

    def __init__(
        self,
        *,
        headless: bool = True,
        timeout_seconds: float = 60.0,
        executable_path: str | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.headless = headless
        self.timeout_ms = int(timeout_seconds * 1000)
        self.executable_path = executable_path
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def open(self, url: str) -> BrowserSnapshot:
        self._ensure_started()
        assert self._page is not None
        try:
            self._navigate_to_list_page(url)
        except Exception as exc:
            raise BrowserPaginationError(
                "公开成交列表页加载超时或未呈现成交记录；请稍后重试，"
                "或在浏览器中确认该公开页面能够正常打开。"
            ) from exc
        return self._snapshot(requested_url=url)

    def next_page(self) -> BrowserSnapshot | None:
        self._ensure_started()
        assert self._page is not None
        candidate = self._find_next_control()
        if candidate is None:
            return None
        next_href = candidate.get_attribute("href")
        if next_href:
            try:
                next_url = normalize_hkp_transaction_list_url(urljoin(self._page.url, next_href))
                if next_url == self._page.url:
                    raise BrowserPaginationError("下一页链接没有指向新的公开列表页。")
                # Follow the exact href exposed by the visible pagination
                # control. This stays on the public page and avoids relying on
                # fragile client-side click completion signals.
                self._navigate_to_list_page(next_url)
            except BrowserPaginationError:
                raise
            except Exception as exc:
                raise BrowserPaginationError("无法读取公开成交列表的下一页。") from exc
        else:
            before_url = self._page.url
            before = self._transaction_link_marker()
            try:
                candidate.click(timeout=self.timeout_ms)
                self._wait_for_transaction_page_update(
                    before_url=before_url,
                    before_marker=before,
                )
            except Exception as exc:
                raise BrowserPaginationError(
                    "点击下一页后，公开成交记录未在预期时间内更新。"
                ) from exc
        return self._snapshot(requested_url=self._page.url)

    def close(self) -> None:
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
        if self._browser is not None:
            try:
                self._browser.close()
            except Exception:
                pass
        if self._playwright is not None:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    def _ensure_started(self) -> None:
        if self._page is not None:
            return
        try:
            module = importlib.import_module("playwright.sync_api")
        except ModuleNotFoundError as exc:
            raise BrowserUnavailableError(
                "未安装 Playwright。请安装项目依赖后运行：python -m playwright install chromium"
            ) from exc
        try:
            self._playwright = module.sync_playwright().start()
            self._browser = self._playwright.chromium.launch(
                headless=self.headless,
                executable_path=self.executable_path,
            )
            self._context = self._browser.new_context(locale="zh-CN")
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.timeout_ms)
        except Exception as exc:
            self.close()
            raise BrowserUnavailableError(
                "无法启动 Playwright 浏览器；请确认 Chromium 已安装且没有被安全策略阻止。"
            ) from exc

    def _find_next_control(self):
        assert self._page is not None
        for selector in self._NEXT_SELECTORS:
            locator = self._page.locator(selector)
            try:
                count = locator.count()
            except Exception:
                continue
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    aria_disabled = (candidate.get_attribute("aria-disabled") or "").casefold()
                    parent_class = (
                        candidate.locator("xpath=..").get_attribute("class") or ""
                    ).casefold()
                    candidate_class = (candidate.get_attribute("class") or "").casefold()
                    if (
                        not candidate.is_visible()
                        or candidate.is_disabled()
                        or aria_disabled == "true"
                        or "disabled" in candidate_class.split()
                        or "disabled" in parent_class.split()
                    ):
                        continue
                    return candidate
                except Exception:
                    continue
        return None

    def _navigate_to_list_page(self, url: str) -> None:
        """Navigate through a visible public list URL and await rendered cards."""

        assert self._page is not None
        self._page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
        # A script element is deliberately not visually rendered. The
        # Playwright default state is ``visible``, which would wait until
        # timeout even when the public Next.js data is already available.
        self._wait_for_rendered_list()

    def _wait_for_rendered_list(self) -> None:
        """Wait until the public page has its Next.js payload and list cards."""

        assert self._page is not None
        # A script element is deliberately not visually rendered. The
        # Playwright default state is ``visible``, which would wait until
        # timeout even when the public Next.js data is already available.
        self._page.wait_for_selector(
            "script#__NEXT_DATA__",
            state="attached",
            timeout=self.timeout_ms,
        )
        # HKP initially serves an empty list shell. The transaction cards are
        # rendered shortly afterwards by the public browser page.
        self._page.wait_for_selector(
            self._PUBLIC_DETAIL_SELECTOR,
            state="attached",
            timeout=self.timeout_ms,
        )

    def _wait_for_transaction_page_update(self, *, before_url: str, before_marker: str) -> None:
        """Wait for either a client-side route change or new transaction links.

        HKP sometimes advances a button-based page through Next.js routing
        before replacing the visible transaction anchors. Treating only an
        anchor change as progress creates false timeout errors on slow pages.
        """

        assert self._page is not None
        self._page.wait_for_function(
            self._PAGE_ADVANCEMENT_SCRIPT,
            arg={"url": before_url, "marker": before_marker},
            timeout=self.timeout_ms,
        )
        self._wait_for_rendered_list()

    def _transaction_link_marker(self) -> str:
        assert self._page is not None
        return self._page.locator(self._PUBLIC_DETAIL_SELECTOR).evaluate_all(
            "anchors => anchors.map(anchor => anchor.href).sort().join('\\n')"
        )

    def _snapshot(self, *, requested_url: str) -> BrowserSnapshot:
        assert self._page is not None
        return BrowserSnapshot(
            requested_url=requested_url,
            final_url=self._page.url,
            html=self._page.content(),
        )

