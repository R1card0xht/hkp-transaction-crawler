from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

from hkp_transaction_crawler.enrichment import LocationEnrichment
from hkp_transaction_crawler.models import DISPLAY_LABELS, EXPORT_COLUMNS, TransactionRecord
from hkp_transaction_crawler.storage import TransactionStore


class StorageTests(unittest.TestCase):
    def test_upsert_and_exact_csv_xlsx_exports(self) -> None:
        # Keep the temporary database under the project directory so this test
        # works in sandboxes that block the system temporary directory.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary_directory:
            root = Path(temporary_directory)
            database = root / "transactions.sqlite"
            csv_output = root / "transactions.csv"
            xlsx_output = root / "transactions.xlsx"
            record = TransactionRecord(
                record_id="I100",
                source_url="https://www.hkp.com.hk/zh-cn/transaction/example-I100",
                transaction_date="2026-10-31",
                price_hkd=10_000_000,
                log_price=16.118095,
                transaction_month_index=82,
                district="将军澳",
                saleable_area_sqft=500,
                log_area=6.214608,
                bedrooms=2,
                completion_year=2015,
                property_age=11,
                property_age_sq=121,
                floor="18",
                floor_band="mid",
                property_type="tower",
                has_balcony=1,
                has_living_balcony=1,
                has_terrace=0,
                has_parking=0,
                has_rooftop=0,
                has_garden=0,
                mtr_walk_min=8.56789,
                public_transport_stops_within_1km=24,
                mall_walk_min=6.123456,
                school_count_within_1km=9,
                nearest_hk8_university_distance_km=4.234567,
                medical_walk_min=11.111119,
                district_price_index=157.22229,
                tx_type="S",
                mkt_type="2",
                latitude=22.3,
                longitude=114.2,
                geo_status="pending",
            )
            with TransactionStore(database) as store:
                self.assertEqual(store.upsert_many([record]), 1)
                self.assertEqual(store.count(), 1)
                self.assertTrue(store.has_record("I100"))
                self.assertEqual(store.get_record("I100").record_id, "I100")
                self.assertEqual(
                    [item.record_id for item in store.records_requiring_geo_refresh("2026-11-01")],
                    ["I100"],
                )
                cached = LocationEnrichment(
                    mtr_walk_min=8.5,
                    public_transport_stops_within_1km=24,
                    mall_walk_min=6.0,
                    school_count_within_1km=9,
                    nearest_hk8_university_distance_km=4.2,
                    medical_walk_min=11.0,
                    geo_snapshot_date="2026-11-01",
                    geo_status="ok",
                )
                store.put_geo_cache("2026-11-01|B1|22.3|114.1", cached)
                self.assertEqual(
                    store.get_geo_cache("2026-11-01|B1|22.3|114.1").mtr_walk_min,
                    8.5,
                )
                self.assertEqual(store.export_csv(csv_output), 1)
                self.assertEqual(store.export_xlsx(xlsx_output), 1)

            with csv_output.open(encoding="utf-8-sig", newline="") as stream:
                reader = csv.DictReader(stream)
                self.assertEqual(reader.fieldnames, EXPORT_COLUMNS)
                rows = list(reader)
            self.assertEqual(rows[0]["record_id"], "I100")
            self.assertEqual(rows[0]["floor"], "18")
            self.assertEqual(rows[0]["floor_band"], "mid")
            self.assertEqual(rows[0]["has_living_balcony"], "1")
            self.assertNotIn("public_transport_stops_within_1km", rows[0])
            self.assertEqual(len(reader.fieldnames or []), 28)
            self.assertEqual(rows[0]["log_price"], "16.1181")
            self.assertEqual(rows[0]["mtr_walk_min"], "8.5679")
            self.assertEqual(rows[0]["mall_walk_min"], "6.1235")
            self.assertEqual(rows[0]["nearest_hk8_university_distance_km"], "4.2346")
            self.assertEqual(rows[0]["medical_walk_min"], "11.1111")
            self.assertEqual(rows[0]["district_price_index"], "157.2223")

            from openpyxl import load_workbook

            workbook = load_workbook(xlsx_output, data_only=False)
            sheet = workbook["回归数据"]
            self.assertEqual(
                [cell.value for cell in sheet[1]],
                EXPORT_COLUMNS,
            )
            self.assertEqual(
                [cell.value for cell in sheet[2]],
                [DISPLAY_LABELS[column] for column in EXPORT_COLUMNS],
            )
            self.assertEqual(sheet["A3"].value, "I100")
            self.assertEqual(sheet["D3"].value, 10_000_000)
            self.assertEqual(sheet["E3"].value, 16.1181)
            self.assertEqual(sheet["N3"].value, "18")
            self.assertEqual(sheet["O3"].value, "mid")
            self.assertEqual(sheet.max_column, 28)
            self.assertEqual(sheet.auto_filter.ref, "A2:AB3")
            self.assertEqual(sheet["E3"].number_format, "#,##0.####")
            self.assertEqual(sheet["W3"].value, 8.5679)
            self.assertEqual(sheet["X3"].value, 6.1235)
            self.assertFalse(
                any(
                    isinstance(cell.value, str) and cell.value.startswith("=")
                    for row in sheet.iter_rows(min_row=3)
                    for cell in row
                )
            )

    def test_defaults_missing_floor_to_na_and_migrates_legacy_27_column_table(self) -> None:
        import sqlite3
        from hkp_transaction_crawler.storage import DATABASE_COLUMNS

        legacy_columns = [col for col in DATABASE_COLUMNS if col != "floor"]
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as temporary_directory:
            root = Path(temporary_directory)
            database = root / "legacy.sqlite"
            conn = sqlite3.connect(database)
            col_defs = ", ".join(
                f"{col} TEXT PRIMARY KEY" if col == "record_id" else f"{col} TEXT"
                for col in legacy_columns
            )
            conn.execute(f"CREATE TABLE transactions ({col_defs})")
            conn.execute(
                "INSERT INTO transactions (record_id, source_url, floor_band, raw_json) VALUES (?, ?, ?, ?)",
                ("I1", "https://www.hkp.com.hk/zh-cn/transaction/a-I1", "high", '{"floor":"28"}'),
            )
            conn.execute(
                "INSERT INTO transactions (record_id, source_url, floor_band, raw_json) VALUES (?, ?, ?, ?)",
                ("I2", "https://www.hkp.com.hk/zh-cn/transaction/a-I2", "low", '{"floor":null}'),
            )
            conn.commit()
            conn.close()

            with TransactionStore(database) as store:
                self.assertEqual(store.count(), 2)
                self.assertEqual(store.get_record("I1").floor, "28")
                self.assertEqual(store.get_record("I2").floor, "NA")

