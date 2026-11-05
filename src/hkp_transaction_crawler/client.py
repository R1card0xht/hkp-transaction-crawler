"""Small, conservative HTTP client for public HKP pages."""

from __future__ import annotations

import time
from dataclasses import dataclass
from http.client import HTTPException
from typing import Final
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse, urlsplit, urlunsplit
from urllib.request import Request, build_opener
from urllib.robotparser import RobotFileParser


ALLOWED_HOSTS: Final = {"www.hkp.com.hk", "hkp.com.hk"}
DEFAULT_USER_AGENT: Final = "HKPTransactionResearchBot/0.1 (public-page collector; robots-aware)"


class ClientError(RuntimeError):
    """Base class for expected, user-actionable collection failures."""


class InvalidTargetError(ClientError):
    pass


class RobotsDeniedError(ClientError):
    pass


class AccessRestrictedError(ClientError):
    pass


class FetchError(ClientError):
    pass


@dataclass(frozen=True)
class FetchResult:
    requested_url: str
    final_url: str
    status_code: int
    text: str


class HKPClient:
    """Fetch only HTTPS pages on hkp.com.hk, with robots and rate limiting."""

    def __init__(
        self,
        *,
        delay_seconds: float = 3.0,
        timeout_seconds: float = 30.0,
        retries: int = 3,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if delay_seconds < 0:
            raise ValueError("delay_seconds must be non-negative")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.retries = max(0, retries)
        self.user_agent = user_agent
        self._opener = build_opener()
        self._robots: dict[str, RobotFileParser] = {}
        self._last_request_started: float | None = None

    def allowed_by_robots(self, url: str) -> bool:
        """Load the site's robots.txt once and check this exact URL."""

        url = self._normalise_url(url)
        parsed = self._validate_target(url)
        host = (parsed.hostname or "").lower()
        robot = self._robots.get(host)
        if robot is None:
            robots_url = f"https://{host}/robots.txt"
            result = self._request(robots_url)
            if result.status_code != 200:
                raise RobotsDeniedError(
                    f"无法读取 {robots_url}（HTTP {result.status_code}）；为避免误采集，程序已停止。"
                )
            robot = RobotFileParser()
            robot.set_url(robots_url)
            robot.parse(result.text.splitlines())
            self._robots[host] = robot
        return robot.can_fetch(self.user_agent, url)

    def fetch_page(self, url: str) -> FetchResult:
        """Fetch a public page after an affirmative robots.txt check."""

        url = self._normalise_url(url)
        self._validate_target(url)
        if not self.allowed_by_robots(url):
            raise RobotsDeniedError(f"robots.txt 不允许抓取：{url}")
        result = self._request(url)
        self._validate_target(result.final_url)
        self._raise_if_access_restricted(result)
        return result

    def _validate_target(self, url: str):
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if (
            parsed.scheme.lower() != "https"
            or host not in ALLOWED_HOSTS
            or parsed.port not in {None, 443}
        ):
            raise InvalidTargetError(
                "只允许 HTTPS 的 hkp.com.hk 公共页面；不会请求图片、第三方地图或其他网站。"
            )
        return parsed

    @staticmethod
    def _normalise_url(url: str) -> str:
        """Accept browser-copied Chinese URLs without double-encoding % escapes."""

        parsed = urlsplit(url.strip())
        return urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                quote(parsed.path, safe="/%:@-._~!$&'()*+,;="),
                quote(parsed.query, safe="=&%/:?+,;@-._~!$'()*"),
                "",  # Fragments are client-side only and not sent to the site.
            )
        )

    def _request(self, url: str) -> FetchResult:
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            self._respect_delay()
            request = Request(
                url,
                headers={
                    "User-Agent": self.user_agent,
                    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
                },
            )
            try:
                with self._opener.open(request, timeout=self.timeout_seconds) as response:
                    body = response.read()
                    charset = response.headers.get_content_charset() or "utf-8"
                    text = body.decode(charset, errors="replace")
                    return FetchResult(
                        requested_url=url,
                        final_url=response.geturl(),
                        status_code=getattr(response, "status", 200),
                        text=text,
                    )
            except HTTPError as exc:
                if exc.code in {401, 403, 429}:
                    raise AccessRestrictedError(
                        f"网站以 HTTP {exc.code} 拒绝或限制了请求，程序不会重试或绕过限制。"
                    ) from exc
                last_error = exc
                if exc.code < 500 or attempt >= self.retries:
                    break
            except (URLError, TimeoutError, OSError, HTTPException) as exc:
                last_error = exc
                if attempt >= self.retries:
                    break

            # A bounded, courteous retry for transient failures only.
            time.sleep(min(10.0, 1.5 * (2**attempt)))

        detail = str(last_error) if last_error else "unknown error"
        raise FetchError(f"无法读取 {url}：{detail}") from last_error

    def _respect_delay(self) -> None:
        if self._last_request_started is not None:
            elapsed = time.monotonic() - self._last_request_started
            remaining = self.delay_seconds - elapsed
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_started = time.monotonic()

    @staticmethod
    def _raise_if_access_restricted(result: FetchResult) -> None:
        page = result.text.casefold()
        markers = (
            "captcha",
            "verify you are human",
            "access denied",
            "unusual traffic",
            "cf-chl-",
        )
        if any(marker in page for marker in markers):
            raise AccessRestrictedError(
                "页面显示了验证码或访问限制；程序已停止，不会尝试绕过。"
            )

