"""SQLite persistence plus CSV/XLSX export for the final model schema."""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date
from numbers import Real
from pathlib import Path
from typing import Any, Iterable

from .models import (
    DISPLAY_LABELS,
    EXPORT_COLUMNS,
    INTERNAL_COLUMNS,
    PERSISTENCE_ONLY_COLUMNS,
    TransactionRecord,
)


DATABASE_COLUMNS = [*EXPORT_COLUMNS, *PERSISTENCE_ONLY_COLUMNS, *INTERNAL_COLUMNS]
_TABLE_NAME = "transactions"
_GEO_CACHE_TABLE = "geographic_factor_cache"

_INTEGER_COLUMNS = {
    "transaction_month_index",
    "bedrooms",
    "completion_year",
    "property_age",
    "property_age_sq",
    "has_balcony",
    "has_living_balcony",
    "has_terrace",
    "has_parking",
    "has_rooftop",
    "has_garden",
    "public_transport_stops_within_1km",
    "school_count_within_1km",
}
_REAL_COLUMNS = {
    "price_hkd",
    "log_price",
    "saleable_area_sqft",
    "log_area",
    "mtr_walk_min",
    "mall_walk_min",
    "nearest_hk8_university_distance_km",
    "medical_walk_min",
    "district_price_index",
    "latitude",
    "longitude",
}
_DATE_COLUMNS = {"transaction_date"}


def _column_type(name: str) -> str:
    if name in _INTEGER_COLUMNS:
        return "INTEGER"
    if name in _REAL_COLUMNS:
        return "REAL"
    return "TEXT"


class TransactionStore:
    """Upsert model-ready records by public HKP record ID."""

    def __init__(self, database_path: str | Path) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self._create_schema()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "TransactionStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def upsert(self, record: TransactionRecord) -> None:
        self.upsert_many([record])

    def upsert_many(self, records: Iterable[TransactionRecord]) -> int:
        rows = [record.to_dict() for record in records]
        if not rows:
            return 0

        column_sql = ", ".join(DATABASE_COLUMNS)
        placeholders = ", ".join("?" for _ in DATABASE_COLUMNS)
        update_columns = [column for column in DATABASE_COLUMNS if column != "record_id"]
        updates = ", ".join(f"{column}=excluded.{column}" for column in update_columns)
        statement = (
            f"INSERT INTO {_TABLE_NAME} ({column_sql}) VALUES ({placeholders}) "
            f"ON CONFLICT(record_id) DO UPDATE SET {updates}"
        )
        values = [
            [
                _normalize_floor(row.get(column)) if column == "floor" else row.get(column)
                for column in DATABASE_COLUMNS
            ]
            for row in rows
        ]
        with self.connection:
            self.connection.executemany(statement, values)
        return len(rows)

    def export_csv(self, csv_path: str | Path) -> int:
        """Export the exact A:AA schema as UTF-8-BOM CSV."""

        target = Path(csv_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self._export_rows()
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=EXPORT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    def export_xlsx(self, xlsx_path: str | Path) -> int:
        """Export a reviewable, formula-free Excel workbook.

        The first row has machine-readable field names.  The second row mirrors
        the Chinese description row in the user's original input template.
        Data begin on row 3 and contain materialized values, never formulas.
        """

        try:
            from openpyxl import Workbook
            from openpyxl.styles import Alignment, Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError as exc:  # pragma: no cover - environment-specific
            raise RuntimeError(
                "XLSX 输出需要 openpyxl。请运行 `python -m pip install openpyxl`。"
            ) from exc

        target = Path(xlsx_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        rows = self._export_rows()

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "回归数据"
        sheet.freeze_panes = "A3"
        sheet.append(EXPORT_COLUMNS)
        sheet.append([DISPLAY_LABELS[column] for column in EXPORT_COLUMNS])

        dark_blue = PatternFill(fill_type="solid", fgColor="1F4E78")
        pale_blue = PatternFill(fill_type="solid", fgColor="D9EAF7")
        for cell in sheet[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = dark_blue
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        for cell in sheet[2]:
            cell.font = Font(bold=True)
            cell.fill = pale_blue
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)

        for row in rows:
            sheet.append([_excel_value(column, row.get(column)) for column in EXPORT_COLUMNS])

        # Use the human-readable description row as the filter header.  This
        # leaves the technical header intact for data-import workflows.
        last_column_letter = get_column_letter(len(EXPORT_COLUMNS))
        sheet.auto_filter.ref = f"A2:{last_column_letter}{max(2, sheet.max_row)}"
        sheet.row_dimensions[1].height = 24
        sheet.row_dimensions[2].height = 42
        for index, column in enumerate(EXPORT_COLUMNS, start=1):
            letter = get_column_letter(index)
            sheet.column_dimensions[letter].width = _column_width(column)
            for row_number in range(3, sheet.max_row + 1):
                cell = sheet.cell(row=row_number, column=index)
                cell.alignment = Alignment(vertical="top")
                _apply_number_format(cell, column)

        workbook.save(target)
        return len(rows)

    def count(self) -> int:
        return int(self.connection.execute(f"SELECT COUNT(*) FROM {_TABLE_NAME}").fetchone()[0])

    def has_record(self, record_id: str) -> bool:
        """Return whether a public HKP transaction ID is already persisted."""

        row = self.connection.execute(
            f"SELECT 1 FROM {_TABLE_NAME} WHERE record_id = ? LIMIT 1", (record_id,)
        ).fetchone()
        return row is not None

    def get_record(self, record_id: str) -> TransactionRecord | None:
        """Load one stored record, including internal context needed to refresh it."""

        row = self.connection.execute(
            f"SELECT {', '.join(DATABASE_COLUMNS)} FROM {_TABLE_NAME} WHERE record_id = ?",
            (record_id,),
        ).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["floor"] = _normalize_floor(data.get("floor"))
        return TransactionRecord(**data)

    def records_requiring_geo_refresh(self, snapshot_date: str) -> list[TransactionRecord]:
        """Return coordinate-bearing records that have incomplete geographic factors.

        Records that already completed geographic enrichment (including via the
        Google Places railway fallback when the official railway source was
        unavailable) are not re-fetched on resume runs.
        """

        rows = self.connection.execute(
            f"SELECT {', '.join(DATABASE_COLUMNS)} FROM {_TABLE_NAME} "
            "WHERE latitude IS NOT NULL AND longitude IS NOT NULL "
            "AND ("
            "geo_snapshot_date IS NULL "
            "OR geo_status IS NULL "
            "OR geo_status = 'pending' "
            "OR geo_status = 'coordinate_missing' "
            "OR geo_status LIKE '%google_maps_unavailable%' "
            "OR geo_status LIKE '%_failed%' "
            "OR (geo_snapshot_date != ? AND ("
            "mtr_walk_min IS NULL OR mall_walk_min IS NULL "
            "OR school_count_within_1km IS NULL "
            "OR nearest_hk8_university_distance_km IS NULL "
            "OR medical_walk_min IS NULL"
            "))"
            ") "
            "ORDER BY transaction_date DESC, record_id",
            (snapshot_date,),
        ).fetchall()
        result: list[TransactionRecord] = []
        for row in rows:
            data = dict(row)
            data["floor"] = _normalize_floor(data.get("floor"))
            result.append(TransactionRecord(**data))
        return result

    def all_records(self) -> list[TransactionRecord]:
        """Load every persisted record for a lightweight non-geographic refresh."""

        rows = self.connection.execute(
            f"SELECT {', '.join(DATABASE_COLUMNS)} FROM {_TABLE_NAME} "
            "ORDER BY transaction_date DESC, record_id"
        ).fetchall()
        result: list[TransactionRecord] = []
        for row in rows:
            data = dict(row)
            data["floor"] = _normalize_floor(data.get("floor"))
            result.append(TransactionRecord(**data))
        return result

    def get_geo_cache(self, cache_key: str) -> Any | None:
        """Return a cacheable ``LocationEnrichment`` without raw Places data."""

        row = self.connection.execute(
            f"SELECT value_json FROM {_GEO_CACHE_TABLE} WHERE cache_key = ?", (cache_key,)
        ).fetchone()
        if row is None:
            parts = cache_key.split("|")
            if len(parts) == 6:
                coord_suffix = "|".join(parts[3:])
                row = self.connection.execute(
                    f"SELECT value_json FROM {_GEO_CACHE_TABLE} "
                    "WHERE cache_key LIKE ? ORDER BY updated_at DESC LIMIT 1",
                    (f"{parts[0]}|%||{coord_suffix}".replace("||", "|%|"),),
                ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row["value_json"])
            from .enrichment import LocationEnrichment

            return LocationEnrichment(**payload)
        except (ImportError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def put_geo_cache(self, cache_key: str, enrichment: Any) -> None:
        """Persist only calculated geographic factors for a same-coordinate reuse."""

        values = {
            "mtr_walk_min": enrichment.mtr_walk_min,
            "public_transport_stops_within_1km": enrichment.public_transport_stops_within_1km,
            "mall_walk_min": enrichment.mall_walk_min,
            "school_count_within_1km": enrichment.school_count_within_1km,
            "nearest_hk8_university_distance_km": enrichment.nearest_hk8_university_distance_km,
            "medical_walk_min": enrichment.medical_walk_min,
            "geo_snapshot_date": enrichment.geo_snapshot_date,
            "geo_status": enrichment.geo_status,
        }
        with self.connection:
            self.connection.execute(
                f"INSERT INTO {_GEO_CACHE_TABLE} (cache_key, value_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cache_key) DO UPDATE SET "
                "value_json=excluded.value_json, updated_at=excluded.updated_at",
                (cache_key, json.dumps(values, ensure_ascii=False), date.today().isoformat()),
            )

    def _export_rows(self) -> list[dict[str, Any]]:
        column_sql = ", ".join(EXPORT_COLUMNS)
        cursor = self.connection.execute(
            f"SELECT {column_sql} FROM {_TABLE_NAME} "
            "ORDER BY transaction_date DESC, record_id"
        )
        return [
            {
                column: (
                    _normalize_floor(row[column])
                    if column == "floor"
                    else _round_export_value(row[column])
                )
                for column in EXPORT_COLUMNS
            }
            for row in cursor.fetchall()
        ]

    def _create_schema(self) -> None:
        existing = [row[1] for row in self.connection.execute(f"PRAGMA table_info({_TABLE_NAME})")]
        if existing and set(existing) != set(DATABASE_COLUMNS):
            if set(existing) == set(DATABASE_COLUMNS) - {"floor"}:
                self._migrate_add_floor_column(existing)
                return
            # A previous version used a semantically different record model.
            # Preserve rather than silently delete it, then create the exact
            # A:AB export schema plus persistence-only fields used by the
            # current program.
            legacy_name = self._next_legacy_table_name()
            with self.connection:
                self.connection.execute(f"ALTER TABLE {_TABLE_NAME} RENAME TO {legacy_name}")

        definitions = []
        for column in DATABASE_COLUMNS:
            definition = f"{column} {_column_type(column)}"
            if column == "record_id":
                definition += " PRIMARY KEY"
            definitions.append(definition)
        with self.connection:
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {_TABLE_NAME} (" + ", ".join(definitions) + ")"
            )
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {_GEO_CACHE_TABLE} ("
                "cache_key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def _migrate_add_floor_column(self, existing_columns: list[str]) -> None:
        """Upgrade a 27-column table in-place and backfill ``floor`` from ``raw_json``."""

        temp_table = f"{_TABLE_NAME}_migrating_floor"
        definitions = []
        for column in DATABASE_COLUMNS:
            definition = f"{column} {_column_type(column)}"
            if column == "record_id":
                definition += " PRIMARY KEY"
            definitions.append(definition)

        rows = self.connection.execute(
            f"SELECT {', '.join(existing_columns)} FROM {_TABLE_NAME}"
        ).fetchall()
        migrated_values: list[list[Any]] = []
        for row in rows:
            data = dict(row)
            data["floor"] = _floor_from_raw_json(data.get("raw_json"))
            migrated_values.append([data.get(col) for col in DATABASE_COLUMNS])

        column_sql = ", ".join(DATABASE_COLUMNS)
        placeholders = ", ".join("?" for _ in DATABASE_COLUMNS)
        with self.connection:
            self.connection.execute(f"DROP TABLE IF EXISTS {temp_table}")
            self.connection.execute(f"CREATE TABLE {temp_table} (" + ", ".join(definitions) + ")")
            if migrated_values:
                self.connection.executemany(
                    f"INSERT INTO {temp_table} ({column_sql}) VALUES ({placeholders})",
                    migrated_values,
                )
            self.connection.execute(f"DROP TABLE {_TABLE_NAME}")
            self.connection.execute(f"ALTER TABLE {temp_table} RENAME TO {_TABLE_NAME}")
            self.connection.execute(
                f"CREATE TABLE IF NOT EXISTS {_GEO_CACHE_TABLE} ("
                "cache_key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
            )

    def _next_legacy_table_name(self) -> str:
        candidate = "transactions_legacy"
        suffix = 1
        existing = {
            row[0]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        while candidate in existing:
            candidate = f"transactions_legacy_{suffix}"
            suffix += 1
        return candidate


def _normalize_floor(value: Any) -> str:
    if value is None:
        return "NA"
    text = str(value).strip()
    if not text or text.upper() in {"-", "--", "N/A", "NA", "NONE", "NULL", "UNKNOWN"}:
        return "NA"
    return text


def _floor_from_raw_json(raw_json: Any) -> str:
    if not raw_json or not isinstance(raw_json, str):
        return "NA"
    try:
        payload = json.loads(raw_json)
    except json.JSONDecodeError:
        return "NA"
    if not isinstance(payload, dict):
        return "NA"
    return _normalize_floor(payload.get("floor"))


def _excel_value(column: str, value: Any) -> Any:
    if column == "floor":
        return _normalize_floor(value)
    if value is None or value == "":
        return None
    if column in _DATE_COLUMNS and isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return value
    return value


def _round_export_value(value: Any) -> Any:
    """Keep CSV/XLSX numeric values at no more than four decimal places."""

    if isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, Real):
        return round(float(value), 4)
    return value


def _apply_number_format(cell: Any, column: str) -> None:
    if column in _DATE_COLUMNS:
        cell.number_format = "yyyy-mm-dd"
    elif column in _INTEGER_COLUMNS:
        cell.number_format = "#,##0"
    elif column in _REAL_COLUMNS:
        cell.number_format = "#,##0.####"


def _column_width(column: str) -> float:
    if column == "source_url":
        return 52
    if column in {"record_id", "district", "floor_band", "property_type"}:
        return 18
    if column in {"transaction_date", "completion_year", "floor"}:
        return 14
    if column == "nearest_hk8_university_distance_km":
        return 30
    return 20

