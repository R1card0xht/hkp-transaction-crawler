from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from hkp_transaction_crawler.tasks import TaskFileError, normalize_hkp_transaction_list_url, read_tasks_csv


class TaskFileTests(unittest.TestCase):
    def _write(self, text: str) -> Path:
        directory = Path(tempfile.mkdtemp(dir=Path.cwd()))
        path = directory / "tasks.csv"
        path.write_text(text, encoding="utf-8-sig")
        self.addCleanup(lambda: shutil.rmtree(directory, ignore_errors=True))
        return path

    def test_read_tasks_normalizes_and_deduplicates(self) -> None:
        path = self._write(
            "hkp_list_url\n"
            "https://www.hkp.com.hk/zh-cn/list/transaction/天晋-E-E000010411\n"
            "# keep this note\n"
            "https://www.hkp.com.hk/zh-cn/list/transaction/天晋-E-E000010411\n"
        )

        tasks = read_tasks_csv(path)

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].row_number, 2)
        self.assertIn("%E5%A4%A9%E6%99%8B", tasks[0].hkp_list_url)

    def test_rejects_wrong_header(self) -> None:
        path = self._write("url\nhttps://www.hkp.com.hk/zh-cn/list/transaction/example\n")
        with self.assertRaisesRegex(TaskFileError, "hkp_list_url"):
            read_tasks_csv(path)

    def test_explains_when_an_xlsx_workbook_is_renamed_to_csv(self) -> None:
        path = self._write("placeholder\n")
        path.write_bytes(b"PK\x03\x04not-a-text-csv")

        with self.assertRaisesRegex(TaskFileError, "实际内容是 Excel .xlsx"):
            read_tasks_csv(path)

    def test_reads_legacy_chinese_windows_csv(self) -> None:
        path = self._write("placeholder\n")
        path.write_bytes(
            (
                "hkp_list_url\n"
                "https://www.hkp.com.hk/zh-cn/list/transaction/天晋-E-E000010411\n"
            ).encode("gb18030")
        )

        tasks = read_tasks_csv(path)

        self.assertEqual(len(tasks), 1)
        self.assertIn("%E5%A4%A9%E6%99%8B", tasks[0].hkp_list_url)

    def test_rejects_detail_url_and_other_hosts(self) -> None:
        with self.assertRaises(TaskFileError):
            normalize_hkp_transaction_list_url("https://www.hkp.com.hk/zh-cn/transaction/example-I1")
        with self.assertRaises(TaskFileError):
            normalize_hkp_transaction_list_url("https://example.com/zh-cn/list/transaction/example")

