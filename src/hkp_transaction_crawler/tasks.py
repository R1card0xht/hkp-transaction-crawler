"""Validation and loading for the one-column collection task file.

The user-facing task file intentionally stays small: one public HKP transaction
list URL per row. This module does not perform any network calls.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit


ALLOWED_HKP_HOSTS = {"www.hkp.com.hk", "hkp.com.hk"}
REQUIRED_TASK_HEADER = "hkp_list_url"


class TaskFileError(ValueError):
    """The collection task file is missing, malformed, or unsafe to use."""


@dataclass(frozen=True)
class CollectionTask:
    """One approved public transaction-list URL from ``tasks.csv``."""

    hkp_list_url: str
    row_number: int


def read_tasks_csv(path: str | Path) -> list[CollectionTask]:
    """Read, validate, and de-duplicate a one-column task CSV.

    The header must be exactly ``hkp_list_url``. Blank rows and rows whose only
    value starts with ``#`` are ignored. A duplicate URL is retained only once
    so it cannot cause redundant pagination work.
    """

    source = Path(path)
    if not source.is_file():
        raise TaskFileError(f"找不到任务文件：{source}")

    try:
        with source.open("rb") as binary_stream:
            signature = binary_stream.read(4)
    except OSError as exc:
        raise TaskFileError(f"无法读取任务文件：{source}") from exc

    if signature == b"PK\x03\x04":
        raise TaskFileError(
            "tasks.csv 的实际内容是 Excel .xlsx 工作簿，不能只把扩展名改为 .csv。"
            "请在 Excel 中选择“另存为”→“CSV UTF-8（逗号分隔）(*.csv)”，"
            "再覆盖 tasks.csv。"
        )

    text = _read_task_csv_text(source)
    stream = io.StringIO(text, newline="")
    with stream:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise TaskFileError("tasks.csv 为空；第一行必须是 hkp_list_url。") from exc

        _validate_header(header)
        tasks: list[CollectionTask] = []
        seen: set[str] = set()
        for row_number, row in enumerate(reader, start=2):
            if not row or not any(cell.strip() for cell in row):
                continue
            if len(row) != 1:
                raise TaskFileError(f"第 {row_number} 行必须只有 hkp_list_url 一列。")
            raw_url = row[0].strip()
            if not raw_url or raw_url.startswith("#"):
                continue
            url = normalize_hkp_transaction_list_url(raw_url)
            if url in seen:
                continue
            seen.add(url)
            tasks.append(CollectionTask(hkp_list_url=url, row_number=row_number))

    if not tasks:
        raise TaskFileError("tasks.csv 没有可执行任务；请至少填写一个成交列表页 URL。")
    return tasks


def _read_task_csv_text(source: Path) -> str:
    """Read a real text CSV, including the legacy Chinese Windows encoding."""

    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return source.read_text(encoding=encoding)
        except UnicodeDecodeError:
            continue
        except OSError as exc:
            raise TaskFileError(f"无法读取任务文件：{source}") from exc
    raise TaskFileError(
        "tasks.csv 必须是 UTF-8 或中文 Windows CSV（GB18030）文本；"
        "请在 Excel 中选择“CSV UTF-8（逗号分隔）(*.csv)”后重试。"
    )


def normalize_hkp_transaction_list_url(url: str) -> str:
    """Validate and normalize a browser-copied HKP transaction list URL."""

    parsed = urlsplit(url.strip())
    hostname = (parsed.hostname or "").lower()
    try:
        port = parsed.port
    except ValueError as exc:
        raise TaskFileError(f"URL 的端口无效：{url}") from exc

    if (
        parsed.scheme.lower() != "https"
        or hostname not in ALLOWED_HKP_HOSTS
        or port not in {None, 443}
    ):
        raise TaskFileError("任务 URL 必须是 https://www.hkp.com.hk 的公开成交列表页。")

    normalized_path = quote(parsed.path, safe="/%:@-._~!$&'()*+,;=")
    path_for_check = normalized_path.casefold()
    if "/list/transaction" not in path_for_check:
        raise TaskFileError("任务 URL 必须指向香港置业的 /list/transaction 成交列表页。")

    normalized_query = quote(parsed.query, safe="=&%/:?+,;@-._~!$'()*")
    return urlunsplit(("https", hostname, normalized_path, normalized_query, ""))


def _validate_header(header: list[str]) -> None:
    if len(header) != 1 or header[0].strip() != REQUIRED_TASK_HEADER:
        raise TaskFileError("tasks.csv 第一行必须且只能是：hkp_list_url")

