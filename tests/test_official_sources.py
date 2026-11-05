from __future__ import annotations

import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import Workbook

from hkp_transaction_crawler.official_sources import (
    OfficialSourceLoader,
    SCHOOL_LOCATION_URL,
    parse_hk1980_stop_xml,
    parse_geojson_pois,
    parse_gtfs_stops,
    parse_school_location_workbook,
    parse_ugc_hk8_csv,
)
from hkp_transaction_crawler.geospatial_core import PoiRecord


class OfficialSourceParserTests(unittest.TestCase):
    @staticmethod
    def _school_workbook_bytes() -> bytes:
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(
            [
                "SCHOOL NO.",
                "ENGLISH NAME",
                "ENGLISH ADDRESS",
                "SCHOOL LEVEL",
                "SESSION",
                "LATITUDE",
                "LONGITUDE",
            ]
        )
        sheet.append(["100111", "Example School", "1 Example Road", "KINDERGARTEN", "A.M.", 22.3, 114.1])
        sheet.append(["100112", "Example School", "1 Example Road", "KINDERGARTEN", "P.M.", 22.3, 114.1])
        sheet.append(["100113", "Example School", "1 Example Road", "KINDERGARTEN", "WHOLE DAY", 22.3, 114.1])
        sheet.append(["100223", "Example School", "1 Example Road", "PRIMARY", "WHOLE DAY", 22.3, 114.1])
        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    def test_parses_gtfs_stop_coordinates(self) -> None:
        payload = (
            "stop_id,stop_name,stop_lat,stop_lon\n"
            "1,测试站,22.3000,114.1700\n"
        ).encode("utf-8")
        result = parse_gtfs_stops(payload)
        self.assertEqual(result[0].source_id, "transport:1")
        self.assertEqual(result[0].latitude, 22.3)

    def test_parses_hk1980_auxiliary_transport_stops_with_injected_transform(self) -> None:
        payload = (
            "<?xml version='1.0'?><dataroot><STOP>"
            "<STOP_ID>20000003</STOP_ID><X>836561</X><Y>815610</Y>"
            "</STOP></dataroot>"
        ).encode("utf-8")
        result = parse_hk1980_stop_xml(
            payload,
            source_prefix="transport:gmb",
            coordinate_transformer=lambda x, y: (22.3, 114.2),
        )
        self.assertEqual(result[0].source_id, "transport:gmb:20000003")
        self.assertEqual((result[0].latitude, result[0].longitude), (22.3, 114.2))

    def test_parses_geojson_and_keeps_properties_for_category_filter(self) -> None:
        payload = json.dumps(
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"id": "x", "name": "MTR Exit", "type": "Railway Station Access"},
                        "geometry": {"type": "Point", "coordinates": [114.17, 22.3]},
                    }
                ],
            }
        ).encode("utf-8")
        result = parse_geojson_pois(payload, source_prefix="test")
        self.assertEqual(result[0].source_id, "test:x")
        self.assertIn("Railway", result[0].category or "")

    def test_school_parser_assigns_one_institution_key_per_name_address_and_level(self) -> None:
        result = parse_school_location_workbook(self._school_workbook_bytes())

        self.assertEqual(len(result), 4)
        self.assertEqual({item.physical_key for item in result[:3]}, {result[0].physical_key})
        self.assertNotEqual(result[0].physical_key, result[3].physical_key)

    def test_school_loader_uses_only_the_education_bureau_register(self) -> None:
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            loader = OfficialSourceLoader(Path(folder))
            with patch.object(loader, "_cached_bytes", return_value=self._school_workbook_bytes()) as cached:
                result = loader._load_schools()

        self.assertEqual(len(result), 4)
        cached.assert_called_once_with("school_locations.xlsx", SCHOOL_LOCATION_URL)

    def test_parses_ugc_chinese_header(self) -> None:
        payload = "設施名稱,緯度,經度\n香港大學,22.2842,114.1381\n".encode("utf-8")
        result = parse_ugc_hk8_csv(payload)
        self.assertEqual(result[0].name, "香港大學")
        self.assertAlmostEqual(result[0].longitude, 114.1381)

    def test_ugc_parser_keeps_only_one_named_hk8_main_campus_per_institution(self) -> None:
        payload = (
            "設施名稱,緯度,經度\n"
            "香港大學,22.2842,114.1381\n"
            "香港大學（附屬設施）,22.2850,114.1390\n"
            "非八大專上院校,22.3000,114.1500\n"
        ).encode("utf-8")
        result = parse_ugc_hk8_csv(payload)
        self.assertEqual([item.source_id for item in result], ["ugc:hku"])

    def test_loader_assigns_transport_to_declared_snapshot_field(self) -> None:
        poi = PoiRecord("source:1", 22.3, 114.1)
        with TemporaryDirectory(dir=Path.cwd()) as folder:
            loader = OfficialSourceLoader(Path(folder))
            with (
                patch.object(loader, "_load_schools", return_value=[poi]),
                patch.object(loader, "_load_public_transport", return_value=[poi]),
                patch.object(loader, "_load_railway_accesses", return_value=[poi]),
                patch.object(loader, "_load_hk8_main_campuses", return_value=[poi]),
            ):
                snapshot = loader.load_current()
        self.assertEqual(snapshot.public_transport_stops, [poi])
        self.assertEqual(snapshot.source_errors, {})

