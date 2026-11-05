"""Orchestration for collection from public HKP transaction list pages.

List pagination is deliberately delegated to :mod:`.browser`, which drives
the site's visible public controls. Detail pages are then retrieved through
the robots-aware :class:`.client.HKPClient`; this module never constructs or
calls a private HKP endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
from typing import Callable, Iterable, Protocol
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .browser import ListPageNavigator, PlaywrightPublicListNavigator, PublicListPaginator
from .client import AccessRestrictedError, ClientError, HKPClient, InvalidTargetError, RobotsDeniedError
from .models import TransactionRecord
from .parser import ParseError, parse_page
from .storage import TransactionStore
from .tasks import CollectionTask
from .time_utils import hong_kong_today


ProgressCallback = Callable[[str], None]
BrowserFactory = Callable[[], ListPageNavigator]


class RecordEnricher(Protocol):
    """Optional hook used to materialize location factors before persistence.

    The concrete geographic layer can be passed directly when it exposes an
    ``enrich(record)`` method. A simple ``Callable[[TransactionRecord],
    TransactionRecord]`` is accepted as well, making this orchestration layer
    independent of a particular enrichment implementation.
    """

    def enrich(self, record: TransactionRecord) -> TransactionRecord: ...


@dataclass(slots=True)
class CrawlSummary:
    """Counters for one resumable collection run."""

    task_count: int = 0
    list_pages_fetched: int = 0
    list_records_seen: int = 0
    detail_pages_fetched: int = 0
    records_upserted: int = 0
    records_skipped_existing: int = 0
    records_skipped_not_sale: int = 0
    records_skipped_not_secondary: int = 0
    records_skipped_outside_date_range: int = 0
    records_skipped_missing_date: int = 0
    records_skipped_non_residential: int = 0
    detail_urls_rejected: int = 0
    detail_parse_failures: int = 0
    index_records_refreshed: int = 0
    geographic_records_refreshed: int = 0
    google_requests_used: int = 0
    stopped_at_record_limit: bool = False


def crawl_tasks(
    tasks: Iterable[CollectionTask],
    *,
    client: HKPClient,
    store: TransactionStore,
    source_cache: str | Path,
    max_records: int | None = 500,
    google_max_requests: int = 100_000,
    enricher: RecordEnricher | Callable[[TransactionRecord], TransactionRecord] | None = None,
    navigator_factory: BrowserFactory | None = None,
    progress: ProgressCallback | None = None,
    start_date: date = date(2023, 1, 1),
    end_date: date | None = None,
) -> CrawlSummary:
    """Collect eligible detail records discoverable from ``tasks``.

    Only records satisfying all agreed study restrictions are stored:

    * transaction date from 2023-01-01 through today's Hong Kong date;
    * sale transactions (``tx_type == S``);
    * secondary-market transactions (``mkt_type == 2``); and
    * recognisably residential building types.

    Collection stops cleanly once ``max_records`` (default 500) records have
    been recorded in this run. Existing SQLite record IDs are not fetched
    again, so a completed batch or an interrupted run can safely resume from
    where it left off.
    """

    if max_records is not None and max_records < 1:
        raise ValueError("max_records 必须至少为 1。")
    if google_max_requests < 1:
        raise ValueError("google_max_requests 必须至少为 1。")
    if end_date is None:
        end_date = _hong_kong_today()
    if start_date > end_date:
        raise ValueError("start_date 不可晚于 end_date。")

    task_list = list(tasks)
    if not task_list:
        raise ValueError("tasks.csv 没有可执行任务。")

    # The caller's enrichment factory may use this path for official-source
    # snapshots. Creating it here also makes the CLI's documented argument
    # useful when enrichment is injected later.
    Path(source_cache).mkdir(parents=True, exist_ok=True)

    notify = progress or (lambda _: None)
    make_navigator = navigator_factory or (lambda: PlaywrightPublicListNavigator())
    summary = CrawlSummary(task_count=len(task_list))
    existing_ids = _existing_record_ids(store)
    existing_detail_urls = _existing_detail_url_keys(store)
    seen_detail_urls: set[str] = set()

    try:
        for task in task_list:
            notify(f"浏览器读取成交列表：{task.hkp_list_url}")
            navigator = make_navigator()
            paginator = PublicListPaginator(
                navigator,
                robots_allowed=client.allowed_by_robots,
                delay_seconds=client.delay_seconds,
            )

            for page in paginator.iter_pages(task.hkp_list_url):
                summary.list_pages_fetched += 1
                parsed = parse_page(page.html, page.final_url, _now_utc())
                if parsed.kind != "list":
                    raise ParseError("浏览器分页返回的不是香港置业公开成交列表页。")

                summary.list_records_seen += len(parsed.records)
                records_by_detail_url = _records_by_detail_url(parsed.records, page.final_url)

                for raw_detail_url in parsed.detail_urls:
                    detail_url = _public_detail_url(raw_detail_url, page.final_url)
                    if detail_url is None:
                        summary.detail_urls_rejected += 1
                        continue
                    detail_key = _url_key(detail_url)
                    if detail_key in seen_detail_urls:
                        continue
                    seen_detail_urls.add(detail_key)

                    # A stored canonical detail URL is the most reliable
                    # resume key. HKP currently mixes I…, NO…, and numeric
                    # public slug suffixes, so not every URL exposes a useful
                    # record ID before its page is fetched.
                    if detail_key in existing_detail_urls:
                        summary.records_skipped_existing += 1
                        continue

                    # Where the final slug does expose a record ID, use it as
                    # an additional no-request resume shortcut.
                    url_record_id = _record_id_from_detail_url(detail_url)
                    if url_record_id is not None and url_record_id in existing_ids:
                        summary.records_skipped_existing += 1
                        continue

                    # A list-page record may be enough to avoid an expensive
                    # detail fetch. Missing list fields are deliberately not
                    # treated as failures: then the public detail page decides.
                    list_record = records_by_detail_url.get(detail_key)
                    if list_record is not None:
                        provisional_reason = _provisional_filter_reason(
                            list_record,
                            start_date=start_date,
                            end_date=end_date,
                        )
                        if provisional_reason is not None:
                            _increment_filter_counter(summary, provisional_reason)
                            continue
                        if list_record.record_id in existing_ids:
                            summary.records_skipped_existing += 1
                            continue

                    if max_records is not None and summary.records_upserted >= max_records:
                        summary.stopped_at_record_limit = True
                        break

                    notify(f"读取成交详情：{detail_url}")
                    try:
                        result = client.fetch_page(detail_url)
                    except (AccessRestrictedError, RobotsDeniedError, InvalidTargetError):
                        raise
                    except ClientError as exc:
                        summary.detail_parse_failures += 1
                        notify(f"跳过读取失败的成交详情（下次运行将继续尝试）：{detail_url}（{exc}）")
                        continue
                    summary.detail_pages_fetched += 1
                    try:
                        detail = parse_page(result.text, result.final_url, _now_utc())
                    except ParseError as exc:
                        summary.detail_parse_failures += 1
                        notify(f"跳过无法解析的成交详情：{result.final_url}（{exc}）")
                        continue

                    if detail.kind != "detail":
                        summary.detail_parse_failures += 1
                        notify(f"跳过非详情成交页面：{result.final_url}")
                        continue

                    for record in detail.records:
                        reason = _final_filter_reason(
                            record,
                            start_date=start_date,
                            end_date=end_date,
                        )
                        if reason is not None:
                            _increment_filter_counter(summary, reason)
                            continue
                        if record.record_id in existing_ids:
                            summary.records_skipped_existing += 1
                            continue

                        # Persist the parsed HKP record immediately so that if
                        # an interruption occurs during geographic enrichment,
                        # the next run resumes enrichment without re-fetching
                        # the HKP detail page.
                        store.upsert(record)
                        existing_ids.add(record.record_id)
                        existing_detail_urls.add(detail_key)
                        existing_detail_urls.add(_url_key(record.source_url))

                        final_record = _apply_enricher(enricher, record)
                        store.upsert(final_record)
                        summary.records_upserted += 1
                        if max_records is not None and summary.records_upserted >= max_records:
                            summary.stopped_at_record_limit = True
                            break

                if summary.stopped_at_record_limit:
                    break
            if summary.stopped_at_record_limit:
                notify(f"已达到本次运行记录上限（{max_records} 条），停止采集。")
                break
    finally:
        summary.google_requests_used = _google_requests_used(enricher)

    return summary


def _existing_record_ids(store: TransactionStore) -> set[str]:
    rows = store.connection.execute("SELECT record_id FROM transactions")
    return {str(row[0]) for row in rows if row[0] is not None}


def _existing_detail_url_keys(store: TransactionStore) -> set[str]:
    """Return canonical detail URLs already committed to the SQLite store."""

    rows = store.connection.execute("SELECT source_url FROM transactions")
    return {_url_key(str(row[0])) for row in rows if row[0] is not None}


def _records_by_detail_url(
    records: Iterable[TransactionRecord],
    page_url: str,
) -> dict[str, TransactionRecord]:
    indexed: dict[str, TransactionRecord] = {}
    for record in records:
        detail_url = _public_detail_url(record.source_url, page_url)
        if detail_url is not None:
            indexed.setdefault(_url_key(detail_url), record)
    return indexed


def _public_detail_url(value: str, base_url: str) -> str | None:
    """Resolve one rendered link while keeping collection on HKP detail pages."""

    candidate = urljoin(base_url, value)
    try:
        parsed = urlsplit(candidate)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or host not in {"www.hkp.com.hk", "hkp.com.hk"}
        or port not in {None, 443}
        or not _is_public_transaction_detail_path(parsed.path)
    ):
        return None
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))


def _is_public_transaction_detail_path(path: str) -> bool:
    """Return whether ``path`` is /{locale}/transaction/{single-record-slug}."""

    parts = [part for part in unquote(path).split("/") if part]
    return len(parts) == 3 and parts[1].casefold() == "transaction" and bool(parts[2])


def _url_key(url: str) -> str:
    """Canonical-enough identity for de-duplicating browser-discovered links."""

    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.casefold(), host, path, parsed.query, ""))


def _record_id_from_detail_url(url: str) -> str | None:
    """Return an HKP public I… or NO… record ID when the slug exposes one."""

    final_slug = unquote(urlsplit(url).path).rstrip("/").rsplit("/", 1)[-1]
    match = re.search(r"(?:^|-)\s*((?:I|NO)[A-Za-z0-9]+)$", final_slug, flags=re.IGNORECASE)
    return match.group(1) if match else None


def _provisional_filter_reason(
    record: TransactionRecord,
    *,
    start_date: date,
    end_date: date,
) -> str | None:
    """Return only failures that a potentially sparse list record proves."""

    if str(record.tx_type or "").strip() and not _is_sale(record):
        return "not_sale"
    if str(record.mkt_type or "").strip() and not _is_secondary_market(record):
        return "not_secondary"
    record_date = _record_date(record)
    if record.transaction_date is not None:
        if record_date is None:
            # Let the detail record distinguish a malformed date from a
            # list-page serialization quirk.
            return None
        if not start_date <= record_date <= end_date:
            return "outside_date_range"
    return None


def _final_filter_reason(
    record: TransactionRecord,
    *,
    start_date: date,
    end_date: date,
) -> str | None:
    if not _is_sale(record):
        return "not_sale"
    if not _is_secondary_market(record):
        return "not_secondary"
    record_date = _record_date(record)
    if record_date is None:
        return "missing_date"
    if not start_date <= record_date <= end_date:
        return "outside_date_range"
    if not _is_residential(record):
        return "non_residential"
    return None


def _is_sale(record: TransactionRecord) -> bool:
    return str(record.tx_type or "").strip().upper() == "S"


def _is_secondary_market(record: TransactionRecord) -> bool:
    value = str(record.mkt_type or "").strip()
    if not value:
        return False
    try:
        return Decimal(value) == Decimal(2)
    except InvalidOperation:
        return False


def _record_date(record: TransactionRecord) -> date | None:
    value = (record.transaction_date or "").strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _is_residential(record: TransactionRecord) -> bool:
    """Keep only building types that HKP identifies as residential.

    ``property_type`` is normalized by the parser from known HKP codes. The
    raw fallback handles a few spelling variants while intentionally rejecting
    unknown categories instead of accidentally retaining car parks, offices or
    industrial units.
    """

    if record.property_type in {"tower", "house"}:
        return True
    raw = (record.building_type_raw or "").strip().upper().replace("-", "_").replace(" ", "_")
    if raw in {
        "APARTMENT",
        "FLAT",
        "TOWER",
        "RESIDENTIAL",
        "RESIDENTIAL_UNIT",
        "SERVICE_APARTMENT",
        "HOUSE",
        "VILLA",
        "VILLAGE_HOUSE",
        "TOWNHOUSE",
        "DETACHED_HOUSE",
    }:
        return True
    return any(token in raw for token in ("住宅", "洋房", "村屋", "別墅", "别墅"))


def _increment_filter_counter(summary: CrawlSummary, reason: str) -> None:
    counters = {
        "not_sale": "records_skipped_not_sale",
        "not_secondary": "records_skipped_not_secondary",
        "outside_date_range": "records_skipped_outside_date_range",
        "missing_date": "records_skipped_missing_date",
        "non_residential": "records_skipped_non_residential",
    }
    setattr(summary, counters[reason], getattr(summary, counters[reason]) + 1)


def _apply_enricher(
    enricher: RecordEnricher | Callable[[TransactionRecord], TransactionRecord] | None,
    record: TransactionRecord,
) -> TransactionRecord:
    if enricher is None:
        return record
    method = getattr(enricher, "enrich", None)
    result = method(record) if callable(method) else enricher(record)
    if not isinstance(result, TransactionRecord):
        raise TypeError("enricher 必须返回 TransactionRecord。")
    return result


def _google_requests_used(
    enricher: RecordEnricher | Callable[[TransactionRecord], TransactionRecord] | None,
) -> int:
    """Read the standard geographic client's counter when it is available."""

    service = getattr(enricher, "google_maps", None)
    value = getattr(service, "request_count", 0)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _hong_kong_today() -> date:
    return hong_kong_today()


def _now_utc() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
