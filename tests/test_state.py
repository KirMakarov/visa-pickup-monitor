import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from visa_pickup_monitor import infrastructure as monitor


def legacy_state():
    state = monitor.asdict(monitor.State())
    for key in ("broadcast_baseline", "broadcast_seen", "broadcast_pending"):
        del state[key]
    return state


class StateTests(unittest.TestCase):
    def test_atomic_save_failure_keeps_previous_state_and_removes_temporary_file(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            state = monitor.State(
                found_numbers={"123456": "https://embassy.example/current.pdf"}
            )
            monitor.save_state(state, path)
            previous = path.read_bytes()
            state.notified_numbers.append("123456")
            with patch(
                "visa_pickup_monitor.infrastructure.os.replace",
                side_effect=OSError("Disk error"),
            ):
                with self.assertRaises(monitor.MonitorError):
                    monitor.save_state(state, path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).glob(".state-*.tmp")), [])

    def test_invalid_state_fields_are_rejected_independently(self):
        baseline = {"version": 4, **monitor.asdict(monitor.State())}
        missing_version = baseline.copy()
        del missing_version["version"]
        missing_key = baseline.copy()
        del missing_key["last_targets"]
        extra_key = {**baseline, "unexpected": True}
        examples = (
            ("root is not an object", []),
            ("version is missing", missing_version),
            ("state keys are missing", missing_key),
            ("state has an extra key", extra_key),
            ("version is unsupported", {**baseline, "version": 5}),
            ("boolean is not an integer version", {**baseline, "version": True}),
            ("fraction is not an integer version", {**baseline, "version": 3.0}),
            ("last targets are not a list", {**baseline, "last_targets": None}),
            (
                "last target is not an application number",
                {**baseline, "last_targets": ["invalid"]},
            ),
            (
                "last target is not a string",
                {**baseline, "last_targets": [123456]},
            ),
            (
                "notified numbers are not a list",
                {**baseline, "notified_numbers": None},
            ),
            (
                "notified number is not a string",
                {**baseline, "notified_numbers": [123456]},
            ),
            (
                "notified number has an invalid format",
                {
                    **baseline,
                    "found_numbers": {"123456": "https://source.example/list.pdf"},
                    "notified_numbers": ["invalid"],
                },
            ),
            (
                "found numbers are not an object",
                {**baseline, "found_numbers": ["123456"]},
            ),
            (
                "found number key is invalid",
                {
                    **baseline,
                    "found_numbers": {"invalid": "https://source.example/list.pdf"},
                },
            ),
            (
                "found number URL is not a string",
                {**baseline, "found_numbers": {"123456": None}},
            ),
            (
                "found number URL has an unsupported scheme",
                {**baseline, "found_numbers": {"123456": "file:///list.pdf"}},
            ),
            (
                "notified number has no found history",
                {**baseline, "notified_numbers": ["123456"]},
            ),
            (
                "error notifications are not a list",
                {**baseline, "error_notifications": {}},
            ),
            (
                "error notification is not a string",
                {**baseline, "error_notifications": [1]},
            ),
            (
                "last PDF URL has no processed date",
                {**baseline, "last_pdf_url": "https://source.example/list.pdf"},
            ),
            (
                "processed date has no last PDF URL",
                {**baseline, "last_processed_date": "2026-11-05"},
            ),
            (
                "last PDF URL is not HTTP or HTTPS",
                {
                    **baseline,
                    "last_pdf_url": "file:///list.pdf",
                    "last_processed_date": "2026-11-05",
                },
            ),
            (
                "last PDF URL is not a string",
                {
                    **baseline,
                    "last_pdf_url": 123,
                    "last_processed_date": "2026-11-05",
                },
            ),
            (
                "processed date is invalid while its URL is present",
                {
                    **baseline,
                    "last_pdf_url": "https://source.example/list.pdf",
                    "last_processed_date": "not-a-date",
                },
            ),
            (
                "processed date is not a string",
                {
                    **baseline,
                    "last_pdf_url": "https://source.example/list.pdf",
                    "last_processed_date": 20261105,
                },
            ),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for label, data in examples:
                with self.subTest(label=label):
                    path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaises(monitor.MonitorError):
                        monitor.load_state(path)

    def test_legacy_daily_fields_are_rejected_independently(self):
        baseline = {
            "version": 2,
            **legacy_state(),
            "daily": {
                "date": "2026-11-05",
                "successful": 2,
                "failed": 1,
                "pdf_downloaded": True,
                "errors": [],
                "report_delivered": False,
            },
        }
        daily = baseline["daily"]
        assert isinstance(daily, dict)
        missing_daily_key = daily.copy()
        del missing_daily_key["failed"]
        extra_daily_key = {**daily, "extra": 1}
        examples = (
            ("daily is not an object", []),
            ("daily key is missing", missing_daily_key),
            ("daily has an extra key", extra_daily_key),
            ("date is not a string", {**daily, "date": 20261105}),
            ("date is not valid", {**daily, "date": "2026-02-30"}),
            ("date is not canonical", {**daily, "date": "20261105"}),
            ("successful is not an integer", {**daily, "successful": True}),
            ("successful is negative", {**daily, "successful": -1}),
            ("failed is not an integer", {**daily, "failed": False}),
            ("failed is negative", {**daily, "failed": -1}),
            ("pdf_downloaded is not a boolean", {**daily, "pdf_downloaded": 1}),
            ("errors are not a list", {**daily, "errors": {}}),
            ("error is not a string", {**daily, "errors": [1]}),
            ("report_delivered is not a boolean", {**daily, "report_delivered": 0}),
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for label, invalid_daily in examples:
                with self.subTest(label=label):
                    path.write_text(
                        json.dumps({**baseline, "daily": invalid_daily}),
                        encoding="utf-8",
                    )
                    with self.assertRaises(monitor.MonitorError):
                        monitor.load_state(path)

    def test_supported_state_versions_load_and_legacy_daily_is_optional(self):
        v1 = {"version": 1, **legacy_state()}
        v2_without_daily = {
            "version": 2,
            **legacy_state(),
            "daily": None,
        }
        v2_with_daily = {
            "version": 2,
            **legacy_state(),
            "daily": {
                "date": "2026-11-05",
                "successful": 2,
                "failed": 0,
                "pdf_downloaded": True,
                "errors": [],
                "report_delivered": False,
            },
        }
        v3 = {"version": 3, **legacy_state()}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            for data in (v1, v2_without_daily, v2_with_daily, v3):
                with self.subTest(version=data["version"], daily=data.get("daily")):
                    path.write_text(json.dumps(data), encoding="utf-8")
                    self.assertEqual(monitor.load_state(path), monitor.State())

    def test_cache_date_uses_belgrade_with_winter_and_summer_offsets(self):
        for now, expected in (
            (datetime(2026, 11, 5, 22, 30, tzinfo=timezone.utc), "2026-11-05"),
            (datetime(2026, 11, 5, 23, 30, tzinfo=timezone.utc), "2026-11-06"),
            (datetime(2026, 7, 5, 22, 30, tzinfo=timezone.utc), "2026-07-06"),
        ):
            with self.subTest(now=now):
                self.assertEqual(monitor.belgrade_date(now), expected)
