import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from visa_pickup_monitor import infrastructure as monitor


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
            ("version 1 is unsupported", {**baseline, "version": 1}),
            ("version 2 is unsupported", {**baseline, "version": 2}),
            ("version 3 is unsupported", {**baseline, "version": 3}),
            ("version is unsupported", {**baseline, "version": 5}),
            ("boolean is not an integer version", {**baseline, "version": True}),
            ("float is not an integer version", {**baseline, "version": 4.0}),
            ("string is not an integer version", {**baseline, "version": "4"}),
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

    def test_current_state_round_trip_preserves_history(self):
        state = monitor.State(
            last_pdf_url="https://embassy.example/current.pdf",
            last_processed_date="2026-11-05",
            last_targets=["123456"],
            found_numbers={"123456": "https://embassy.example/discovery.pdf"},
            notified_numbers=["123456"],
            error_notifications=["Source unavailable"],
            broadcast_baseline=True,
            broadcast_seen=["123456 05.11.2026"],
            broadcast_pending=["Pending broadcast"],
        )
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            monitor.save_state(state, path)
            self.assertEqual(json.loads(path.read_text())["version"], 4)
            self.assertEqual(monitor.load_state(path), state)

    def test_cache_date_uses_belgrade_with_winter_and_summer_offsets(self):
        for now, expected in (
            (datetime(2026, 11, 5, 22, 30, tzinfo=timezone.utc), "2026-11-05"),
            (datetime(2026, 11, 5, 23, 30, tzinfo=timezone.utc), "2026-11-06"),
            (datetime(2026, 7, 5, 22, 30, tzinfo=timezone.utc), "2026-07-06"),
        ):
            with self.subTest(now=now):
                self.assertEqual(monitor.belgrade_date(now), expected)
