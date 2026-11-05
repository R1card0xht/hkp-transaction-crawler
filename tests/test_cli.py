from __future__ import annotations

import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from hkp_transaction_crawler.cli import _print_source_warnings, build_parser, main


class CliTests(unittest.TestCase):
    def test_crawl_parser_exposes_final_batch_interface(self) -> None:
        args = build_parser().parse_args(
            [
                "crawl",
                "--tasks",
                "tasks.csv",
                "--database",
                "state.sqlite",
                "--csv",
                "result.csv",
                "--xlsx",
                "result.xlsx",
                "--source-cache",
                "sources",
                "--google-max-requests",
                "1000",
            ]
        )
        self.assertEqual(args.tasks, Path("tasks.csv"))
        self.assertEqual(args.database, Path("state.sqlite"))
        self.assertEqual(args.max_records, 500)
        self.assertEqual(args.google_max_requests, 1000)
        self.assertEqual(args.timeout, 60.0)

    def test_export_command_writes_both_final_formats(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as folder:
            root = Path(folder)
            database = root / "state.sqlite"
            csv_path = root / "result.csv"
            xlsx_path = root / "result.xlsx"
            result = main(
                [
                    "export",
                    "--database",
                    str(database),
                    "--csv",
                    str(csv_path),
                    "--xlsx",
                    str(xlsx_path),
                ]
            )
            self.assertEqual(result, 0)
            self.assertTrue(csv_path.is_file())
            self.assertTrue(xlsx_path.is_file())

    def test_suppresses_railway_source_warning_only(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            _print_source_warnings(
                {
                    "railway_accesses": "temporary source failure",
                    "schools": "temporary source failure",
                }
            )

        rendered = output.getvalue()
        self.assertNotIn("railway_accesses", rendered)
        self.assertIn("schools", rendered)

