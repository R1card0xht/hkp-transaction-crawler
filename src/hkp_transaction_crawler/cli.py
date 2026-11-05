"""Command-line interface for the public-page HKP transaction collector."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable, Sequence

from . import __version__
from .browser import BrowserPaginationError, PlaywrightPublicListNavigator
from .client import ClientError, DEFAULT_USER_AGENT, HKPClient
from .crawler import BrowserFactory, CrawlSummary, crawl_tasks
from .enrichment import GeoEnricher
from .final_enricher import FinalRecordEnricher
from .google_maps import GoogleMapsClient, GoogleMapsError, GoogleMapsLimitReached
from .market_index import MarketIndexError, MarketIndexLoader
from .official_sources import OfficialSourceLoader
from .storage import TransactionStore
from .tasks import TaskFileError, read_tasks_csv


# This deliberately avoids importing the concrete enrichment implementation.
# The application wiring can inject a factory once it has built current official
# source snapshots and an optional Google client.
EnricherFactory = Callable[[argparse.Namespace, TransactionStore], object | None]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hkp-transactions",
        description="从香港置业公开成交列表页收集符合条件的二手住宅买卖记录。",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)

    crawl_parser = subcommands.add_parser(
        "crawl",
        help="从 tasks.csv 的公开成交列表页分页收集，并导出 CSV 与 XLSX",
    )
    crawl_parser.add_argument(
        "--tasks",
        type=Path,
        default=Path("tasks.csv"),
        help="只含 hkp_list_url 一列的 tasks.csv（默认：tasks.csv）",
    )
    crawl_parser.add_argument(
        "--database",
        type=Path,
        default=Path("data/transactions.sqlite"),
        help="SQLite 断点续跑数据库（默认：data/transactions.sqlite）",
    )
    crawl_parser.add_argument(
        "--csv",
        type=Path,
        default=Path("outputs/transactions.csv"),
        help="UTF-8-BOM CSV 输出路径（默认：outputs/transactions.csv）",
    )
    crawl_parser.add_argument(
        "--xlsx",
        type=Path,
        default=Path("outputs/transactions.xlsx"),
        help="Excel 输出路径（默认：outputs/transactions.xlsx）",
    )
    crawl_parser.add_argument(
        "--source-cache",
        type=Path,
        default=Path("data/sources"),
        help="官方地理资料快照缓存目录（默认：data/sources）",
    )
    crawl_parser.add_argument(
        "--max-records",
        type=int,
        default=500,
        help="本次运行最多记录的数据条数（默认：500）",
    )
    crawl_parser.add_argument(
        "--google-max-requests",
        type=int,
        default=100_000,
        help="本次运行 Google Places／Routes 安全请求上限（默认：100000）",
    )
    _add_network_arguments(crawl_parser)
    crawl_parser.set_defaults(handler=_run_crawl)

    export = subcommands.add_parser("export", help="将现有 SQLite 断点导出为 CSV 与 XLSX")
    export.add_argument(
        "--database",
        type=Path,
        default=Path("data/transactions.sqlite"),
        help="SQLite 数据库路径（默认：data/transactions.sqlite）",
    )
    export.add_argument(
        "--csv",
        type=Path,
        default=Path("outputs/transactions.csv"),
        help="CSV 输出路径（默认：outputs/transactions.csv）",
    )
    export.add_argument(
        "--xlsx",
        type=Path,
        default=Path("outputs/transactions.xlsx"),
        help="XLSX 输出路径（默认：outputs/transactions.xlsx）",
    )
    export.set_defaults(handler=_run_export)
    return parser


def _add_network_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--delay",
        type=float,
        default=3.0,
        help="公开页面请求／分页之间的最小间隔秒数（默认：3）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="单次 HKP 请求或浏览器等待超时秒数（默认：60）",
    )
    parser.add_argument(
        "--retries",
        type=int,
        default=3,
        help="仅网络或 5xx 临时错误的额外重试次数（默认：3）",
    )
    parser.add_argument(
        "--user-agent",
        default=DEFAULT_USER_AGENT,
        help="用于 robots.txt 和 HKP 请求的 User-Agent",
    )


def _build_client(args: argparse.Namespace) -> HKPClient:
    return HKPClient(
        delay_seconds=args.delay,
        timeout_seconds=args.timeout,
        retries=args.retries,
        user_agent=args.user_agent,
    )


def _run_crawl(
    args: argparse.Namespace,
    *,
    enricher_factory: EnricherFactory | None = None,
    navigator_factory: BrowserFactory | None = None,
) -> int:
    """Run collection and always refresh exports from the committed SQLite DB.

    ``enricher_factory`` and ``navigator_factory`` make command orchestration
    testable without Playwright/network access and let the application compose
    the optional geographic layer without coupling this module to it.
    """

    tasks = read_tasks_csv(args.tasks)
    client = _build_client(args)
    active_navigator_factory = navigator_factory or (
        lambda: PlaywrightPublicListNavigator(timeout_seconds=args.timeout)
    )
    completed = False
    summary: CrawlSummary | None = None
    stopped_for_google_limit: str | None = None
    interrupted_by_user = False

    with TransactionStore(args.database) as store:
        try:
            enricher = (
                enricher_factory(args, store)
                if enricher_factory is not None
                else _build_final_enricher(args, store, client)
            )
            pre_refreshed = 0
            if isinstance(enricher, FinalRecordEnricher):
                pre_refreshed = _resume_incomplete_geo_records(
                    store,
                    enricher,
                    max_records=getattr(args, "max_records", 500),
                )
            remaining_records = (
                max(0, args.max_records - pre_refreshed)
                if getattr(args, "max_records", None) is not None
                else None
            )
            if remaining_records == 0:
                summary = CrawlSummary(
                    task_count=len(tasks),
                    geographic_records_refreshed=pre_refreshed,
                    stopped_at_record_limit=True,
                )
            else:
                summary = crawl_tasks(
                    tasks,
                    client=client,
                    store=store,
                    source_cache=args.source_cache,
                    max_records=remaining_records,
                    google_max_requests=args.google_max_requests,
                    enricher=enricher,
                    navigator_factory=active_navigator_factory,
                    progress=print,
                )
                summary.geographic_records_refreshed += pre_refreshed
            if isinstance(enricher, FinalRecordEnricher):
                _refresh_persisted_factors(store, enricher, summary)
            completed = True
        except GoogleMapsLimitReached as exc:
            stopped_for_google_limit = str(exc)
        except KeyboardInterrupt:
            interrupted_by_user = True
        finally:
            csv_count, actual_csv = _export_with_lock_fallback(store.export_csv, args.csv)
            xlsx_count, actual_xlsx = _export_with_lock_fallback(store.export_xlsx, args.xlsx)
            if not completed:
                print("采集在完成前停止；已导出 SQLite 中可安全续跑的当前记录。")
                print(f"数据库：{args.database.resolve()}")
                print(f"CSV（共 {csv_count} 条）：{actual_csv.resolve()}")
                print(f"XLSX（共 {xlsx_count} 条）：{actual_xlsx.resolve()}")

    if interrupted_by_user:
        print("采集已手动中断；再次运行相同命令即可从中断的数据接着录。")
        return 130

    if stopped_for_google_limit is not None:
        print(f"Google 请求已安全停止：{stopped_for_google_limit}")
        print("可使用相同命令再次运行；程序会从 SQLite 中尚未完成的记录接着录。")
        return 0

    assert summary is not None
    _print_summary(
        summary,
        database=args.database,
        csv_path=actual_csv,
        xlsx_path=actual_xlsx,
        csv_count=csv_count,
        xlsx_count=xlsx_count,
        max_records=getattr(args, "max_records", 500),
        google_max_requests=args.google_max_requests,
    )
    return 0


def _build_final_enricher(
    args: argparse.Namespace,
    store: TransactionStore,
    client: HKPClient,
) -> FinalRecordEnricher:
    """Build the one current-source enrichment pipeline for this run."""

    print("正在准备官方学校／交通缓存与区域月度指数，请稍候...")
    snapshot = OfficialSourceLoader(args.source_cache).load_current()
    _print_source_warnings(snapshot.source_errors)

    maps = GoogleMapsClient.from_environment(max_requests=args.google_max_requests)
    try:
        market_index = MarketIndexLoader(args.source_cache, client).load_current()
    except MarketIndexError as exc:
        # Do not discard otherwise valid HKP records when a public chart is
        # temporarily unavailable. A missing factor remains a blank value.
        print(f"警告：无法取得官方区域月度指数；district_price_index 将留空。({exc})")
        market_index = None

    geographic = GeoEnricher(
        snapshot,
        maps,
        cache_get=store.get_geo_cache,
        cache_put=store.put_geo_cache,
    )
    return FinalRecordEnricher(geographic, market_index)


def _print_source_warnings(source_errors: dict[str, str]) -> None:
    """Show actionable source warnings, excluding the intentionally silent rail feed."""

    for source_name, message in source_errors.items():
        if source_name == "railway_accesses":
            continue
        print(f"警告：官方资料 {source_name} 不可用；其对应 factor 将留空。({message})")


def _resume_incomplete_geo_records(
    store: TransactionStore,
    enricher: FinalRecordEnricher,
    *,
    max_records: int | None = None,
) -> int:
    """Complete any previously interrupted records before fetching new pages."""

    pending = store.records_requiring_geo_refresh(enricher.geo_snapshot_date)
    if max_records is not None:
        pending = pending[:max_records]
    count = 0
    for record in pending:
        store.upsert(enricher.enrich(record))
        count += 1
    return count


def _refresh_persisted_factors(
    store: TransactionStore,
    enricher: FinalRecordEnricher,
    summary: CrawlSummary,
) -> None:
    """Refresh indices and resume incomplete/current-day geographic rows.

    New records are processed first.  On a later identical invocation, the
    crawler correctly skips existing IDs, and this direct SQLite pass resumes
    only geographic records that did not complete against today's snapshot.
    """

    if enricher.market_index is not None:
        indexed = [enricher.index_record(record) for record in store.all_records()]
        if indexed:
            store.upsert_many(indexed)
            summary.index_records_refreshed = len(indexed)

    if not summary.stopped_at_record_limit:
        for record in store.records_requiring_geo_refresh(enricher.geo_snapshot_date):
            store.upsert(enricher.enrich(record))
            summary.geographic_records_refreshed += 1


def _export_with_lock_fallback(
    exporter: Callable[[Path], int],
    target: Path,
) -> tuple[int, Path]:
    """Export to ``target``, or to an unlocked sibling file if Excel holds a lock."""

    try:
        return exporter(target), target
    except PermissionError:
        fallback = target.with_name(f"{target.stem}_latest{target.suffix}")
        count = exporter(fallback)
        print(
            f"警告：{target} 正被其他程序（如 Excel）占用，已改为导出至：{fallback.resolve()}"
        )
        return count, fallback


def _run_export(args: argparse.Namespace) -> int:
    with TransactionStore(args.database) as store:
        csv_count, actual_csv = _export_with_lock_fallback(store.export_csv, args.csv)
        xlsx_count, actual_xlsx = _export_with_lock_fallback(store.export_xlsx, args.xlsx)
    print(f"已从 SQLite 导出 {csv_count} 条 CSV 记录：{actual_csv.resolve()}")
    print(f"已从 SQLite 导出 {xlsx_count} 条 XLSX 记录：{actual_xlsx.resolve()}")
    return 0


def _print_summary(
    summary: CrawlSummary,
    *,
    database: Path,
    csv_path: Path,
    xlsx_path: Path,
    csv_count: int,
    xlsx_count: int,
    max_records: int | None = 500,
    google_max_requests: int,
) -> None:
    print("\n完成")
    print(f"列表入口：{summary.task_count}")
    print(f"浏览器列表页：{summary.list_pages_fetched}")
    print(f"列表内可见记录：{summary.list_records_seen}")
    print(f"详情页：{summary.detail_pages_fetched}")
    print(f"本次写入/更新：{summary.records_upserted}")
    if summary.stopped_at_record_limit and max_records is not None:
        print(f"已达到单次记录上限（{max_records} 条）；再次运行相同命令即可从断点接着录后续数据。")
    if summary.index_records_refreshed:
        print(f"区域指数刷新：{summary.index_records_refreshed}")
    if summary.geographic_records_refreshed:
        print(f"地理 factor 刷新／续跑：{summary.geographic_records_refreshed}")
    print(f"断点跳过：{summary.records_skipped_existing}")
    print(
        "筛选跳过："
        f"非买卖 {summary.records_skipped_not_sale}，"
        f"非二手 {summary.records_skipped_not_secondary}，"
        f"日期不符 {summary.records_skipped_outside_date_range}，"
        f"缺少日期 {summary.records_skipped_missing_date}，"
        f"非住宅 {summary.records_skipped_non_residential}"
    )
    if summary.detail_urls_rejected:
        print(f"拒绝非公开详情链接：{summary.detail_urls_rejected}")
    if summary.detail_parse_failures:
        print(f"跳过无法解析的详情页：{summary.detail_parse_failures}")
    print(f"Google 请求：{summary.google_requests_used}/{google_max_requests}")
    print(f"数据库：{database.resolve()}")
    print(f"CSV（共 {csv_count} 条）：{csv_path.resolve()}")
    print(f"XLSX（共 {xlsx_count} 条）：{xlsx_path.resolve()}")


def main(
    argv: Sequence[str] | None = None,
    *,
    enricher_factory: EnricherFactory | None = None,
    navigator_factory: BrowserFactory | None = None,
) -> int:
    """Run the CLI; optional factories are intended for application wiring/tests."""

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "crawl":
            return _run_crawl(
                args,
                enricher_factory=enricher_factory,
                navigator_factory=navigator_factory,
            )
        return int(args.handler(args))
    except (
        BrowserPaginationError,
        ClientError,
        GoogleMapsError,
        TaskFileError,
        ValueError,
        FileNotFoundError,
        OSError,
    ) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
