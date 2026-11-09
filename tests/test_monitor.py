import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from email.message import Message
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

import monitor


def make_pdf(*pages: str) -> bytes:
    """Build text PDFs containing only synthetic test data."""
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            }
        )
        commands = ["BT /F1 10 Tf 40 750 Td 12 TL"]
        for line in text.splitlines():
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"({escaped}) Tj T*")
        commands.append("ET")
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = stream
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env_path = Path(self.temp.name) / ".env"

    def test_dotenv_preserves_leading_zeros_and_deduplicates(self):
        self.env_path.write_text('NUMBERS="000123, 123456,000123"\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            page_url, numbers = monitor.load_config(
                monitor.read_settings(self.env_path)
            )
        self.assertEqual(page_url, monitor.DEFAULT_PAGE_URL)
        self.assertEqual(numbers, ("000123", "123456"))

    def test_environment_overrides_dotenv(self):
        self.env_path.write_text(
            "PAGE_URL=https://file.example/page\nNUMBERS=123456\n", encoding="utf-8"
        )
        settings = {"PAGE_URL": "https://env.example/page", "NUMBERS": "000123"}
        with patch.dict(os.environ, settings, clear=True):
            self.assertEqual(
                monitor.load_config(monitor.read_settings(self.env_path)),
                ("https://env.example/page", ("000123",)),
            )

    def test_missing_or_invalid_numbers_are_configuration_errors(self):
        for value in ("", "12345", "1234567", "abc123", "123456,", "１２３４５６"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"NUMBERS": value}, clear=True),
            ):
                with self.assertRaisesRegex(monitor.MonitorError, "NUMBERS"):
                    monitor.load_config(monitor.read_settings(self.env_path))

    def test_invalid_source_urls_are_configuration_errors(self):
        for url in (
            "file:///tmp/page.html",
            "embassy.example/page",
            "https://[invalid",
        ):
            settings = {"NUMBERS": "123456", "PAGE_URL": url}
            with self.subTest(url=url), patch.dict(os.environ, settings, clear=True):
                with self.assertRaisesRegex(monitor.MonitorError, "PAGE_URL"):
                    monitor.load_config(monitor.read_settings(self.env_path))

    def test_optional_telegram_requires_both_settings(self):
        self.assertIsNone(monitor.load_telegram({}))
        invalid_settings: tuple[dict[str, str | None], ...] = (
            {"TELEGRAM_BOT_TOKEN": "123:test"},
            {"TELEGRAM_CHAT_ID": "456"},
            {"TELEGRAM_BOT_TOKEN": "invalid", "TELEGRAM_CHAT_ID": "456"},
        )
        for settings in invalid_settings:
            with (
                self.subTest(settings=settings),
                self.assertRaises(monitor.MonitorError),
            ):
                monitor.load_telegram(settings)
        config = monitor.load_telegram(
            {
                "TELEGRAM_BOT_TOKEN": "123:test",
                "TELEGRAM_CHAT_ID": "456",
            }
        )
        assert config is not None
        self.assertEqual(config.chat_id, "456")
        self.assertNotIn("123:test", repr(config))


class LinkTests(unittest.TestCase):
    def test_resolves_named_relative_pdf_and_ignores_unrelated_documents(self):
        html = """
            <a href="authorization.pdf">Authorization</a>
            <a href="../files/current.pdf?download=1#page=2">
                <span>Abholliste/ Lista za preuzimanje viza</span> PDF
            </a>
            <a href="../files/current.pdf?download=1">Abholliste</a>
        """
        self.assertEqual(
            monitor.resolve_pdf_url(html, "https://embassy.example/service/pickup"),
            "https://embassy.example/files/current.pdf?download=1",
        )

    def test_serbian_label_is_enough(self):
        html = '<a href="/current.pdf">Lista za preuzimanje viza</a>'
        self.assertEqual(
            monitor.resolve_pdf_url(html, "https://embassy.example/page"),
            "https://embassy.example/current.pdf",
        )

    def test_missing_or_ambiguous_pdf_is_an_error(self):
        examples = (
            '<a href="/other.pdf">Other document</a>',
            '<a href="/one.pdf">Abholliste</a><a href="/two.pdf">Abholliste</a>',
            '<a href="file:///tmp/current.pdf">Abholliste</a>',
        )
        for html in examples:
            with self.subTest(html=html), self.assertRaises(monitor.MonitorError):
                monitor.resolve_pdf_url(html, "https://embassy.example/page")


class EntryTests(unittest.TestCase):
    def test_matches_past_and_future_dates_with_flexible_whitespace(self):
        text = (
            "Header\n123456 01.01.2020 08:00-10:00\n"
            "Pickup instructions. 000123\n01.01.2099\n08:00 - 10:00\n"
        )
        self.assertEqual(monitor.parse_numbers(text), {"123456", "000123"})

    def test_rejects_substrings_and_numbers_outside_valid_entries(self):
        text = (
            "Contact: 123456\n"
            "9123456 10.11.2026 08:00-10:00\n"
            "X123456 10.11.2026 08:00-10:00\n"
            "123456X 10.11.2026 08:00-10:00\n"
            "123456 10.11.2026 08:00-10:000\n"
            "654321 10.11.2026 08:00-10:00\n"
        )
        self.assertEqual(monitor.parse_numbers(text), {"654321"})

    def test_invalid_dates_and_times_do_not_form_valid_entries(self):
        text = "123456 31.02.2026 08:00-10:00\n123456 10.11.2026 25:00-26:00\n"
        self.assertEqual(monitor.parse_numbers(text), set())

    def test_each_invalid_pickup_field_is_rejected_independently(self):
        invalid_entries = (
            "123456 31.02.2026 08:00-10:00",
            "123456 10.11.2026 25:00-10:00",
            "123456 10.11.2026 08:00-25:00",
        )
        for entry in invalid_entries:
            with self.subTest(entry=entry):
                self.assertEqual(monitor.parse_numbers(entry), set())
        self.assertEqual(
            monitor.parse_numbers(
                "123456 01.01.2020 08:00-10:00\n000123 01.01.2099 08:00-10:00"
            ),
            {"123456", "000123"},
        )

    def test_extracts_all_pages_from_a_real_pdf_structure(self):
        pdf = make_pdf(
            "123456 01.01.2020 08:00-10:00\nExplanatory footer",
            "000123 01.01.2099 08:00-10:00\n123456 01.01.2020 08:00-10:00",
        )
        self.assertEqual(monitor.extract_numbers(pdf), {"123456", "000123"})

    def test_unreadable_pdf_is_a_processing_error(self):
        with self.assertRaisesRegex(monitor.MonitorError, "Could not read"):
            monitor.extract_numbers(b"%PDF-1.7\n%%EOF\n")

    def test_empty_or_unrecognized_pdf_is_a_processing_error(self):
        for pdf in (make_pdf(), make_pdf("Application 123456, no pickup date or time")):
            with (
                self.subTest(size=len(pdf)),
                self.assertRaisesRegex(monitor.MonitorError, "no valid pickup entries"),
            ):
                monitor.extract_numbers(pdf)


class Response(BytesIO):
    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state_path = Path(self.temp.name) / "data" / "state.json"

    def run_script(
        self,
        responses,
        numbers="123456,000123",
        telegram=False,
        today="2026-11-05",
        chat_id="456",
        bot_token="123:test",
        requests=None,
        expected_requests=None,
        sleeps=None,
    ):
        stdout, stderr = StringIO(), StringIO()
        settings = {"PAGE_URL": "https://embassy.example/start", "NUMBERS": numbers}
        if telegram:
            settings.update(TELEGRAM_BOT_TOKEN=bot_token, TELEGRAM_CHAT_ID=chat_id)
        response_iter = iter(responses)
        request_log = []
        route_errors = []

        def respond(request, **kwargs):
            request_log.append(request)
            if expected_requests is not None:
                request_index = len(request_log) - 1
                if request_index >= len(expected_requests):
                    route_errors.append(f"Unexpected HTTP request: {request.full_url}")
                else:
                    expected_url = expected_requests[request_index]
                    if request.full_url != expected_url:
                        route_errors.append(
                            f"HTTP request {request_index + 1} was "
                            f"{request.full_url}; expected {expected_url}"
                        )
            if requests is not None:
                requests.append(request)
            response = next(response_iter)
            if isinstance(response, Exception):
                raise response
            return response

        with (
            patch.dict(os.environ, settings, clear=True),
            patch("monitor.dotenv_values", return_value={}),
            patch("monitor.urlopen", side_effect=respond),
            patch(
                "monitor.sleep",
                side_effect=(lambda delay: sleeps.append(delay))
                if sleeps is not None
                else None,
            ),
            patch("monitor.belgrade_date", return_value=today),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = monitor.main(self.state_path)
        if expected_requests is not None:
            if route_errors:
                self.fail("; ".join(route_errors))
            self.assertEqual(
                len(request_log),
                len(expected_requests),
                "Not all expected HTTP requests were made",
            )
        return result, stdout.getvalue(), stderr.getvalue()

    def run_cli(self, cwd, *arguments, env=None):
        return subprocess.run(
            [sys.executable, "-B", str(Path(monitor.__file__).resolve()), *arguments],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
        )

    def responses(self, pdf, pdf_name="current.pdf"):
        html = f'<a href="{pdf_name}">Abholliste</a>'.encode()
        return [
            Response(html, "https://embassy.example/service/"),
            Response(pdf, f"https://embassy.example/service/{pdf_name}"),
        ]

    def write_legacy_state(self, version):
        legacy = {
            "version": version,
            "last_pdf_url": "https://embassy.example/service/current.pdf",
            "last_processed_date": "2026-11-05",
            "last_targets": ["123456", "000123"],
            "found_numbers": {"123456": "https://embassy.example/discovery.pdf"},
            "notified_numbers": ["123456"],
            "error_notifications": [
                "Could not download the source page; check the connection and URL."
            ],
        }
        if version == 2:
            legacy["daily"] = {
                "date": "2026-11-05",
                "successful": 2,
                "failed": 1,
                "pdf_downloaded": True,
                "errors": [],
                "report_delivered": False,
            }
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(legacy))

    def test_complete_check_prints_matches_and_missing_targets(self):
        responses = self.responses(make_pdf("123456 01.01.2020 08:00-10:00"))
        result, stdout, stderr = self.run_script(responses)
        self.assertEqual(result, 0)
        self.assertIn("PDF: https://embassy.example/service/current.pdf", stdout)
        self.assertIn("123456: found in pickup list", stdout)
        self.assertIn("000123: not found in pickup list", stdout)
        self.assertEqual(stderr, "")

    def test_valid_pdf_without_targets_is_a_successful_check(self):
        result, stdout, stderr = self.run_script(
            self.responses(make_pdf("654321 01.01.2099 08:00-10:00"))
        )
        self.assertEqual(result, 0)
        self.assertNotIn(": found in pickup list", stdout)
        self.assertIn("123456: not found in pickup list", stdout)
        self.assertIn("000123: not found in pickup list", stdout)
        self.assertEqual(stderr, "")

    def test_monitor_cli_help_unknown_argument_and_configuration_errors(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            env = {"PATH": os.environ.get("PATH", "")}
            help_result = self.run_cli(root, "--help", env=env)
            self.assertEqual(help_result.returncode, 0)
            self.assertIn("usage:", help_result.stdout.lower())
            self.assertEqual(help_result.stderr, "")

            unknown_result = self.run_cli(root, "--unknown-option", env=env)
            self.assertEqual(unknown_result.returncode, 2)
            self.assertIn("unrecognized arguments", unknown_result.stderr)
            self.assertNotIn("Traceback", unknown_result.stderr)

        cases = (
            ("missing", "PAGE_URL=http://127.0.0.1:1/pickup\n", {}),
            (
                "empty",
                "PAGE_URL=http://127.0.0.1:1/pickup\nNUMBERS=123456\n",
                {"NUMBERS": ""},
            ),
        )
        for name, dotenv, overrides in cases:
            with self.subTest(numbers=name), TemporaryDirectory() as directory:
                root = Path(directory)
                (root / ".env").write_text(dotenv, encoding="utf-8")
                env = {"PATH": os.environ.get("PATH", ""), **overrides}
                result = self.run_cli(root, env=env)
                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("Set NUMBERS", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((root / "data/state.json").exists())

    def test_unique_numbers_on_every_pdf_page_are_found(self):
        pdf = make_pdf(
            "123456 01.01.2020 08:00-10:00",
            "000123 01.01.2099 08:00-10:00",
            "654321 10.11.2026 08:00-10:00",
        )
        result, stdout, stderr = self.run_script(
            self.responses(pdf),
            numbers="123456,000123,654321",
            expected_requests=[
                "https://embassy.example/start",
                "https://embassy.example/service/current.pdf",
            ],
        )
        self.assertEqual(result, 0)
        for number in ("123456", "000123", "654321"):
            self.assertIn(f"{number}: found in pickup list", stdout)
        self.assertEqual(stderr, "")

    def test_redirected_page_resolves_relative_pdf_from_effective_url(self):
        pdf_url = "https://embassy.example/redirected/files/current.pdf"
        requests = []
        responses = [
            Response(
                b'<a href="../files/current.pdf">Abholliste</a>',
                "https://embassy.example/redirected/service/index.html",
            ),
            Response(
                make_pdf("123456 01.01.2020 08:00-10:00"),
                "https://cdn.example/pickup/current.pdf",
            ),
        ]
        result, stdout, stderr = self.run_script(
            responses,
            numbers="123456",
            requests=requests,
            expected_requests=["https://embassy.example/start", pdf_url],
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            [request.full_url for request in requests],
            [
                "https://embassy.example/start",
                pdf_url,
            ],
        )
        self.assertIn("PDF: https://cdn.example/pickup/current.pdf", stdout)
        self.assertEqual(stderr, "")

    def test_pdf_redirect_url_is_saved_and_included_in_notification(self):
        requested_pdf = "https://embassy.example/service/current.pdf"
        actual_pdf = "https://cdn.example/archive/pickup-2026.pdf"
        requests = []
        responses = [
            Response(
                b'<a href="current.pdf">Abholliste</a>',
                "https://embassy.example/service/",
            ),
            Response(make_pdf("123456 01.01.2020 08:00-10:00"), actual_pdf),
            Response(
                b'{"ok":true,"result":{"message_id":1}}',
                "https://api.telegram.org/",
            ),
        ]
        result, _, stderr = self.run_script(
            responses,
            numbers="123456",
            telegram=True,
            requests=requests,
            expected_requests=[
                "https://embassy.example/start",
                requested_pdf,
                "https://api.telegram.org/bot123:test/sendMessage",
            ],
        )
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        state = monitor.load_state(self.state_path)
        self.assertEqual(state.found_numbers["123456"], actual_pdf)
        self.assertNotEqual(requested_pdf, actual_pdf)
        message = json.loads(requests[-1].data.decode("utf-8"))["text"]
        self.assertIn(actual_pdf, message)

    def test_temporary_source_and_pdf_download_failures_are_retried(self):
        page_url = "https://embassy.example/start"
        pdf_url = "https://embassy.example/service/current.pdf"
        scenarios = (
            (
                [
                    URLError("temporary source failure"),
                    Response(
                        b'<a href="current.pdf">Abholliste</a>',
                        "https://embassy.example/service/",
                    ),
                    Response(make_pdf("123456 01.01.2020 08:00-10:00"), pdf_url),
                ],
                [page_url, page_url, pdf_url],
            ),
            (
                [
                    Response(
                        b'<a href="current.pdf">Abholliste</a>',
                        "https://embassy.example/service/",
                    ),
                    URLError("temporary PDF failure"),
                    Response(make_pdf("123456 01.01.2020 08:00-10:00"), pdf_url),
                ],
                [page_url, pdf_url, pdf_url],
            ),
        )
        for index, (responses, expected_urls) in enumerate(scenarios):
            with self.subTest(expected_urls=expected_urls):
                self.state_path = Path(self.temp.name) / f"retry-{index}.json"
                requests, sleeps = [], []
                result, _, stderr = self.run_script(
                    responses,
                    numbers="123456",
                    requests=requests,
                    expected_requests=expected_urls,
                    sleeps=sleeps,
                )
                self.assertEqual(result, 0)
                self.assertEqual(stderr, "")
                self.assertEqual(len(requests), 3)
                self.assertEqual(sleeps, [1])

    def test_non_utf8_page_and_failed_pdf_do_not_complete_or_cache_check(self):
        failures = (
            (
                [
                    Response(b"\xff", "https://embassy.example/service/"),
                ],
                ["https://embassy.example/start"],
                "not valid UTF-8",
            ),
            (
                [
                    Response(
                        b'<a href="current.pdf">Abholliste</a>',
                        "https://embassy.example/service/",
                    ),
                    URLError("offline"),
                    URLError("offline"),
                    URLError("offline"),
                ],
                [
                    "https://embassy.example/start",
                    "https://embassy.example/service/current.pdf",
                    "https://embassy.example/service/current.pdf",
                    "https://embassy.example/service/current.pdf",
                ],
                "Could not download the pickup-list PDF",
            ),
        )
        for index, (responses, expected_urls, error) in enumerate(failures):
            with self.subTest(error=error):
                self.state_path = (
                    Path(self.temp.name) / f"download-failure-{index}.json"
                )
                requests = []
                result, stdout, stderr = self.run_script(
                    responses,
                    numbers="123456",
                    requests=requests,
                    expected_requests=expected_urls,
                )
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertIn(error, stderr)
                self.assertNotIn("123456: not found", stdout)
                self.assertIsNone(
                    monitor.load_state(self.state_path).last_processed_date
                )
                self.assertEqual(len(requests), len(expected_urls))
                recovered, _, recovery_stderr = self.run_script(
                    self.responses(make_pdf("123456 01.01.2020 08:00-10:00")),
                    numbers="123456",
                    expected_requests=[
                        "https://embassy.example/start",
                        "https://embassy.example/service/current.pdf",
                    ],
                )
                self.assertEqual(recovered, 0)
                self.assertEqual(recovery_stderr, "")
                self.assertEqual(
                    monitor.load_state(self.state_path).last_processed_date,
                    "2026-11-05",
                )

    def test_download_failures_return_error_without_false_absence_results(self):
        failures = (
            lambda: URLError("offline"),
            lambda: HTTPError(
                "https://embassy.example/start", 503, "Unavailable", Message(), None
            ),
            lambda: TimeoutError("timed out"),
        )
        for index, failure in enumerate(failures):
            with self.subTest(failure=index):
                result, stdout, stderr = self.run_script([failure() for _ in range(3)])
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertIn("Error: Could not download the source page", stderr)
                self.assertNotIn("Traceback", stderr)

    def test_unrecognized_pdf_returns_error_without_false_absence_results(self):
        result, stdout, stderr = self.run_script(
            self.responses(make_pdf("No pickup entries"))
        )
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("no valid pickup entries", stderr)

    def test_same_pdf_is_skipped_today_and_downloaded_again_tomorrow(self):
        pdf = make_pdf("123456 01.01.2020 08:00-10:00")
        self.assertEqual(self.run_script(self.responses(pdf))[0], 0)
        page = self.responses(pdf)[0]
        result, stdout, _ = self.run_script([page])
        self.assertEqual(result, 0)
        self.assertIn("already processed today", stdout)
        self.assertNotIn("123456: found", stdout)
        result, stdout, _ = self.run_script(self.responses(pdf), today="2026-11-06")
        self.assertEqual(result, 0)
        self.assertIn("Valid application numbers", stdout)

    def test_new_pdf_url_is_processed_on_the_same_day(self):
        self.run_script(self.responses(make_pdf("123456 01.01.2020 08:00-10:00")))
        responses = [
            Response(
                b'<a href="new.pdf">Abholliste</a>', "https://embassy.example/service/"
            ),
            Response(
                make_pdf("000123 01.01.2099 08:00-10:00"),
                "https://embassy.example/new.pdf",
            ),
        ]
        result, stdout, _ = self.run_script(responses)
        self.assertEqual(result, 0)
        self.assertIn("000123: found", stdout)
        self.assertEqual(
            set(monitor.load_state(self.state_path).found_numbers), {"123456", "000123"}
        )

    def test_completed_monitoring_makes_no_network_requests(self):
        self.run_script(
            self.responses(make_pdf("123456 01.01.2020 08:00-10:00")), numbers="123456"
        )
        requests = []
        result, stdout, _ = self.run_script(
            [], numbers="123456", requests=requests, expected_requests=[]
        )
        self.assertEqual(result, 0)
        self.assertIn("Monitoring complete", stdout)
        self.assertEqual(requests, [])

    def test_added_target_reopens_monitoring_and_invalidates_same_day_cache(self):
        pdf = make_pdf("123456 01.01.2020 08:00-10:00\n000123 01.01.2099 08:00-10:00")
        self.run_script(self.responses(pdf), numbers="123456")
        result, stdout, _ = self.run_script(self.responses(pdf))
        self.assertEqual(result, 0)
        self.assertIn("000123: found", stdout)
        self.assertNotIn("123456: found", stdout)
        self.assertEqual(
            set(monitor.load_state(self.state_path).found_numbers), {"123456", "000123"}
        )

    def test_removed_found_target_keeps_discovery_history_when_readded(self):
        first = self.responses(make_pdf("123456 01.01.2020 08:00-10:00"))
        later = self.responses(make_pdf("000123 01.01.2099 08:00-10:00"), "later.pdf")
        self.assertEqual(self.run_script(first)[0], 0)
        self.assertEqual(self.run_script(later, numbers="000123")[0], 0)

        saved = json.loads(self.state_path.read_text())
        self.assertEqual(
            saved["found_numbers"],
            {
                "123456": "https://embassy.example/service/current.pdf",
                "000123": "https://embassy.example/service/later.pdf",
            },
        )
        self.assertEqual(saved["notified_numbers"], [])

        result, stdout, stderr = self.run_script([])
        self.assertEqual(result, 0)
        self.assertIn("Monitoring complete", stdout)
        self.assertEqual(stderr, "")
        restored = json.loads(self.state_path.read_text())
        self.assertEqual(restored["found_numbers"], saved["found_numbers"])
        self.assertEqual(restored["notified_numbers"], [])

    def test_removed_notified_target_is_not_sent_again_when_readded_in_another_chat(
        self,
    ):
        first = self.responses(make_pdf("123456 01.01.2020 08:00-10:00"))
        later = self.responses(make_pdf("000123 01.01.2099 08:00-10:00"), "later.pdf")
        with patch("monitor.send_telegram") as sender:
            self.assertEqual(self.run_script(first, telegram=True)[0], 0)
            sender.assert_called_once()
            self.assertIn("Application number 123456", sender.call_args.args[1])
        with patch("monitor.send_telegram") as sender:
            self.assertEqual(
                self.run_script(later, numbers="000123", telegram=True)[0], 0
            )
            sender.assert_called_once()
            self.assertIn("Application number 000123", sender.call_args.args[1])

        saved = json.loads(self.state_path.read_text())
        self.assertCountEqual(saved["notified_numbers"], ["123456", "000123"])
        self.assertEqual(
            saved["found_numbers"].get("123456"),
            "https://embassy.example/service/current.pdf",
        )
        with patch("monitor.send_telegram") as sender:
            result, stdout, stderr = self.run_script([], telegram=True, chat_id="789")
            self.assertEqual(result, 0)
            self.assertIn("Monitoring complete", stdout)
            self.assertEqual(stderr, "")
            sender.assert_not_called()
        restored = json.loads(self.state_path.read_text())
        self.assertEqual(restored["found_numbers"], saved["found_numbers"])
        self.assertCountEqual(restored["notified_numbers"], ["123456", "000123"])

    def test_failed_processing_is_retried_with_the_same_url_today(self):
        self.assertEqual(
            self.run_script(self.responses(make_pdf("Unrecognized table")))[0], 1
        )
        self.assertIsNone(monitor.load_state(self.state_path).last_processed_date)
        result, _, _ = self.run_script(
            self.responses(make_pdf("123456 01.01.2020 08:00-10:00"))
        )
        self.assertEqual(result, 0)
        self.assertEqual(
            monitor.load_state(self.state_path).last_processed_date, "2026-11-05"
        )

    def test_enabling_telegram_delivers_only_current_unnotified_targets(self):
        pdf = make_pdf("123456 01.01.2020 08:00-10:00\n000123 01.01.2099 08:00-10:00")
        self.run_script(self.responses(pdf))
        with patch("monitor.send_telegram") as sender:
            self.assertEqual(self.run_script([], numbers="123456", telegram=True)[0], 0)
            sender.assert_called_once()
            self.assertIn("Application number 123456", sender.call_args.args[1])
            self.assertIn(
                "https://embassy.example/service/current.pdf", sender.call_args.args[1]
            )
        with patch("monitor.send_telegram") as sender:
            self.assertEqual(
                self.run_script([], numbers="123456", telegram=True, chat_id="789")[0],
                0,
            )
            sender.assert_not_called()
        self.assertEqual(
            monitor.load_state(self.state_path).notified_numbers, ["123456"]
        )

    def test_complete_check_delivers_and_records_a_confirmed_telegram_response(self):
        responses = self.responses(make_pdf("123456 01.01.2020 08:00-10:00"))
        responses.append(
            Response(
                b'{"ok":true,"result":{"message_id":1}}', "https://api.telegram.org/"
            )
        )
        requests = []
        result, stdout, stderr = self.run_script(
            responses,
            numbers="123456",
            telegram=True,
            bot_token="987:configured-token",
            requests=requests,
        )
        self.assertEqual(result, 0)
        self.assertEqual(stderr, "")
        self.assertIn("Monitoring complete", stdout)
        self.assertEqual(
            monitor.load_state(self.state_path).notified_numbers, ["123456"]
        )
        self.assertEqual(
            [request.full_url for request in requests if request.data is not None],
            ["https://api.telegram.org/bot987:configured-token/sendMessage"],
        )

    def test_unsent_matches_remain_pending_without_a_stale_error_queue(self):
        pdf = make_pdf("123456 01.01.2020 08:00-10:00")
        with patch(
            "monitor.send_telegram",
            side_effect=monitor.MonitorError("Telegram unavailable"),
        ):
            self.assertEqual(
                self.run_script(self.responses(pdf), numbers="123456", telegram=True)[
                    0
                ],
                1,
            )
        state = monitor.load_state(self.state_path)
        self.assertIn("123456", state.found_numbers)
        self.assertEqual(state.notified_numbers, [])
        self.assertEqual(state.error_notifications, [])
        with patch("monitor.send_telegram") as sender:
            self.assertEqual(self.run_script([], numbers="123456", telegram=True)[0], 0)
            sender.assert_called_once()
            self.assertIn("Application number", sender.call_args.args[1])

    def test_unconfirmed_telegram_responses_leave_found_number_pending(self):
        bodies = (b'{"ok":1}', b"not json", b"[]", b"\xff")
        for index, body in enumerate(bodies):
            with self.subTest(body=body):
                self.state_path = Path(self.temp.name) / f"unconfirmed-{index}.json"
                pdf = make_pdf("123456 01.01.2020 08:00-10:00")
                self.assertEqual(
                    self.run_script(self.responses(pdf), numbers="123456")[0], 0
                )
                # Up to three attempts for the match and three for its error alert.
                responses = [
                    Response(body, "https://api.telegram.org/") for _ in range(6)
                ]
                result, stdout, stderr = self.run_script(
                    responses, numbers="123456", telegram=True
                )
                self.assertEqual(result, 1)
                self.assertNotIn("Monitoring complete", stdout)
                self.assertIn("Telegram", stderr)
                saved = json.loads(self.state_path.read_text())
                self.assertIn("123456", saved["found_numbers"])
                self.assertEqual(saved["notified_numbers"], [])
                self.assertEqual(saved["error_notifications"], [])

    def test_each_successful_delivery_survives_a_later_delivery_failure(self):
        pdf = make_pdf("123456 01.01.2020 08:00-10:00\n000123 01.01.2099 08:00-10:00")
        self.run_script(self.responses(pdf))
        with patch(
            "monitor.send_telegram",
            side_effect=[None, monitor.MonitorError("Send failed"), None],
        ):
            self.assertEqual(self.run_script([], telegram=True)[0], 1)
        self.assertEqual(
            monitor.load_state(self.state_path).notified_numbers, ["123456"]
        )
        with patch("monitor.send_telegram") as sender:
            self.assertEqual(self.run_script([], telegram=True)[0], 0)
            sender.assert_called_once()
            self.assertIn("000123", sender.call_args.args[1])
        self.assertEqual(monitor.load_state(self.state_path).error_notifications, [])

    def test_confirmed_delivery_survives_interruption_before_the_next_delivery(self):
        pdf = make_pdf("123456 01.01.2020 08:00-10:00\n000123 01.01.2099 08:00-10:00")
        self.assertEqual(self.run_script(self.responses(pdf))[0], 0)
        with patch(
            "monitor.send_telegram", side_effect=[None, KeyboardInterrupt()]
        ) as sender:
            with self.assertRaises(KeyboardInterrupt):
                self.run_script([], telegram=True)
            self.assertEqual(sender.call_count, 2)
            self.assertIn("Application number 123456", sender.call_args_list[0].args[1])
            self.assertIn("Application number 000123", sender.call_args_list[1].args[1])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["notified_numbers"], ["123456"])
        self.assertEqual(saved["error_notifications"], [])
        with patch("monitor.send_telegram") as sender:
            result, _, stderr = self.run_script([], telegram=True)
            self.assertEqual(result, 0)
            self.assertEqual(stderr, "")
            sender.assert_called_once()
            self.assertIn("Application number 000123", sender.call_args.args[1])
        self.assertCountEqual(
            json.loads(self.state_path.read_text())["notified_numbers"],
            ["123456", "000123"],
        )

    def test_pending_delivery_is_attempted_even_if_the_source_page_fails(self):
        self.run_script(self.responses(make_pdf("123456 01.01.2020 08:00-10:00")))
        with patch("monitor.send_telegram") as sender:
            result, _, _ = self.run_script(
                [URLError("offline") for _ in range(3)], telegram=True
            )
            self.assertEqual(result, 1)
            texts = [call.args[1] for call in sender.call_args_list]
            self.assertTrue(any("Application number 123456" in text for text in texts))
            self.assertTrue(any("Monitor error" in text for text in texts))
        self.assertEqual(
            monitor.load_state(self.state_path).notified_numbers, ["123456"]
        )

    def test_save_failure_after_confirmed_delivery_keeps_disk_history_and_allows_retry(
        self,
    ):
        pdf = make_pdf("123456 01.01.2020 08:00-10:00")
        self.assertEqual(self.run_script(self.responses(pdf), numbers="123456")[0], 0)
        original = self.state_path.read_bytes()
        replace = os.replace
        failed = False

        def fail_notification_save(source, destination):
            nonlocal failed
            saved = json.loads(Path(source).read_text())
            if saved["notified_numbers"] and not failed:
                failed = True
                raise OSError("Synthetic disk failure after confirmed delivery")
            return replace(source, destination)

        with (
            patch("monitor.os.replace", side_effect=fail_notification_save),
            patch("monitor.send_telegram") as sender,
        ):
            result, stdout, stderr = self.run_script(
                [], numbers="123456", telegram=True
            )
            self.assertEqual(result, 1)
            self.assertIn("Could not save data/state.json", stderr)
            self.assertNotIn("Monitoring complete", stdout)
            self.assertEqual(sender.call_count, 2)
            self.assertIn("Application number 123456", sender.call_args_list[0].args[1])
            self.assertIn("Monitor error", sender.call_args_list[1].args[1])
        self.assertTrue(failed)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(json.loads(original)["notified_numbers"], [])
        with patch("monitor.send_telegram") as sender:
            result, _, stderr = self.run_script([], numbers="123456", telegram=True)
            self.assertEqual(result, 0)
            self.assertEqual(stderr, "")
            sender.assert_called_once()
            self.assertIn("Application number 123456", sender.call_args.args[1])
        self.assertEqual(
            json.loads(self.state_path.read_text())["notified_numbers"], ["123456"]
        )

    def test_distinct_errors_are_once_per_episode_and_success_silently_resets_it(self):
        download_error = (
            "Could not download the source page; check the connection and URL."
        )
        pdf_error = "The PDF contains no valid pickup entries; check its format."
        for index, message in enumerate((download_error, pdf_error) * 2):
            responses = (
                [URLError("offline") for _ in range(3)]
                if message == download_error
                else self.responses(make_pdf("No pickup entries"))
            )
            with patch("monitor.send_telegram") as sender:
                result, _, stderr = self.run_script(responses, telegram=True)
                self.assertEqual(result, 1)
                self.assertIn(message, stderr)
                self.assertEqual(
                    [call.args[1] for call in sender.call_args_list],
                    [f"Visa Pickup Monitor error:\n{message}"] if index < 2 else [],
                )
        self.assertCountEqual(
            json.loads(self.state_path.read_text())["error_notifications"],
            [download_error, pdf_error],
        )
        with patch("monitor.send_telegram") as sender:
            result, _, _ = self.run_script(
                self.responses(make_pdf("654321 01.01.2099 08:00-10:00")), telegram=True
            )
            self.assertEqual(result, 0)
            sender.assert_not_called()
        self.assertEqual(
            json.loads(self.state_path.read_text())["error_notifications"], []
        )
        with patch("monitor.send_telegram") as sender:
            self.assertEqual(
                self.run_script([URLError("offline") for _ in range(3)], telegram=True)[
                    0
                ],
                1,
            )
            sender.assert_called_once()

    def test_failed_error_alert_is_not_recorded_or_replayed_after_recovery(self):
        for attempt in range(2):
            with (
                self.subTest(attempt=attempt),
                patch(
                    "monitor.send_telegram",
                    side_effect=monitor.MonitorError("Telegram unavailable"),
                ) as sender,
            ):
                result, _, stderr = self.run_script(
                    [URLError("offline") for _ in range(3)], telegram=True
                )
                self.assertEqual(result, 1)
                self.assertIn("Error alert could not be sent", stderr)
                sender.assert_called_once()
                self.assertIn("Monitor error", sender.call_args.args[1])
                self.assertEqual(
                    json.loads(self.state_path.read_text())["error_notifications"], []
                )
        with patch("monitor.send_telegram") as sender:
            result, _, _ = self.run_script(
                self.responses(make_pdf("654321 01.01.2099 08:00-10:00")), telegram=True
            )
            self.assertEqual(result, 0)
            sender.assert_not_called()

    def test_state_save_failure_prevents_match_delivery_and_reports_the_error(self):
        with (
            patch(
                "monitor.save_state",
                side_effect=monitor.MonitorError("Could not save data/state.json."),
            ),
            patch("monitor.send_telegram") as sender,
        ):
            result, _, _ = self.run_script(
                self.responses(make_pdf("123456 01.01.2020 08:00-10:00")), telegram=True
            )
            self.assertEqual(result, 1)
            sender.assert_called_once()
            self.assertIn("Monitor error", sender.call_args.args[1])
            self.assertNotIn("Application number", sender.call_args.args[1])

    def test_corrupt_state_is_not_overwritten_or_treated_as_empty(self):
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text("broken state", encoding="utf-8")
        with patch("monitor.send_telegram") as sender:
            result, stdout, _ = self.run_script([], telegram=True)
            self.assertEqual(result, 1)
            self.assertEqual(stdout, "")
            sender.assert_called_once()
        self.assertEqual(self.state_path.read_text(), "broken state")

    def test_invalid_cached_targets_fail_without_changing_state_or_requesting_source(
        self,
    ):
        state = {
            "version": 3,
            **monitor.asdict(monitor.State(last_targets=["invalid"])),
        }
        self.state_path.parent.mkdir(parents=True)
        original = json.dumps(state, indent=2).encode("utf-8")
        self.state_path.write_bytes(original)
        requests = []
        result, stdout, stderr = self.run_script(
            self.responses(make_pdf("123456 01.01.2020 08:00-10:00")),
            requests=requests,
        )
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("Invalid state; restore data/state.json", stderr)
        self.assertNotIn("Traceback", stderr)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(requests, [])

    def test_legacy_state_preserves_found_and_notified_history_when_upgraded(self):
        for version in (1, 2):
            with self.subTest(version=version):
                self.write_legacy_state(version)
                result, _, stderr = self.run_script(
                    [
                        self.responses(b"")[0],
                        Response(
                            make_pdf("654321 01.01.2020 08:00-10:00"),
                            "https://embassy.example/current.pdf",
                        ),
                    ],
                    expected_requests=["https://embassy.example/start"],
                )
                self.assertEqual(result, 0)
                self.assertEqual(stderr, "")

                saved = json.loads(self.state_path.read_text())
                self.assertEqual(
                    saved["found_numbers"],
                    {"123456": "https://embassy.example/discovery.pdf"},
                )
                self.assertEqual(saved["notified_numbers"], ["123456"])
                self.assertEqual(saved["version"], 3)
                self.assertNotIn("daily", saved)

    def test_legacy_state_suppresses_previously_delivered_error_alert(self):
        for version in (1, 2):
            with (
                self.subTest(version=version),
                patch("monitor.send_telegram") as sender,
            ):
                self.write_legacy_state(version)
                result, _, stderr = self.run_script(
                    [URLError("offline")] * 3, telegram=True
                )
                self.assertEqual(result, 1)
                self.assertIn("Could not download the source page", stderr)
                sender.assert_not_called()

    def test_legacy_state_preserves_cache_and_silently_clears_failure_episode(self):
        for version in (1, 2):
            with (
                self.subTest(version=version),
                patch("monitor.send_telegram") as sender,
            ):
                self.write_legacy_state(version)
                result, output, stderr = self.run_script(
                    [
                        self.responses(b"")[0],
                        Response(
                            make_pdf("654321 01.01.2020 08:00-10:00"),
                            "https://embassy.example/current.pdf",
                        ),
                    ],
                    telegram=True,
                    expected_requests=["https://embassy.example/start"],
                )
                self.assertEqual(result, 0)
                self.assertIn("already processed", output)
                self.assertEqual(stderr, "")
                sender.assert_not_called()
                self.assertEqual(
                    json.loads(self.state_path.read_text())["error_notifications"], []
                )

    def test_completed_legacy_state_is_upgraded_without_requests(self):
        state = monitor.asdict(
            monitor.State(
                found_numbers={"123456": "https://embassy.example/list.pdf"},
                notified_numbers=["123456"],
            )
        )
        state["daily"] = None
        self.state_path.parent.mkdir(parents=True)
        self.state_path.write_text(json.dumps({"version": 2, **state}))
        with patch("monitor.send_telegram") as sender:
            self.assertEqual(self.run_script([], numbers="123456", telegram=True)[0], 0)
            sender.assert_not_called()
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["version"], 3)
        self.assertNotIn("daily", saved)
        self.assertEqual(saved["notified_numbers"], ["123456"])

    def test_corrupt_reporting_state_is_not_overwritten(self):
        state = {
            "version": 2,
            **monitor.asdict(monitor.State()),
            "daily": {"date": "bad"},
        }
        self.state_path.parent.mkdir(parents=True)
        original = json.dumps(state)
        self.state_path.write_text(original)
        self.assertEqual(self.run_script([])[0], 1)
        self.assertEqual(self.state_path.read_text(), original)

    def test_multiple_findings_and_quiet_checks_across_day_boundary(self):
        targets = "123456,000123,999999"
        pdf = make_pdf("123456 01.01.2020 08:00-10:00\n000123 01.01.2099 08:00-10:00")

        def ok():
            return Response(b'{"ok":true}', "https://api.telegram.org/")

        requests = []
        result, output, _ = self.run_script(
            self.responses(pdf) + [ok(), ok()],
            numbers=targets,
            telegram=True,
            requests=requests,
        )
        self.assertEqual(result, 0)
        messages = [
            json.loads(request.data)["text"] for request in requests if request.data
        ]
        self.assertEqual(
            messages,
            [
                "Application number 123456 found in the pickup list.\nPDF: https://embassy.example/service/current.pdf",
                "Application number 000123 found in the pickup list.\nPDF: https://embassy.example/service/current.pdf",
            ],
        )
        self.assertNotIn("daily report", output.lower())
        for today, responses in (
            ("2026-11-05", [self.responses(pdf)[0]]),
            ("2026-11-06", self.responses(pdf)),
        ):
            requests = []
            result, output, _ = self.run_script(
                responses,
                numbers=targets,
                telegram=True,
                today=today,
                requests=requests,
            )
            self.assertEqual(result, 0)
            self.assertFalse(any(request.data for request in requests))
            self.assertNotIn("daily report", output.lower())


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
            with patch("monitor.os.replace", side_effect=OSError("Disk error")):
                with self.assertRaises(monitor.MonitorError):
                    monitor.save_state(state, path)
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(Path(directory).glob(".state-*.tmp")), [])

    def test_invalid_state_fields_are_rejected_independently(self):
        baseline = {"version": 3, **monitor.asdict(monitor.State())}
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
            ("version is unsupported", {**baseline, "version": 4}),
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
            **monitor.asdict(monitor.State()),
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
        v1 = {"version": 1, **monitor.asdict(monitor.State())}
        v2_without_daily = {
            "version": 2,
            **monitor.asdict(monitor.State()),
            "daily": None,
        }
        v2_with_daily = {
            "version": 2,
            **monitor.asdict(monitor.State()),
            "daily": {
                "date": "2026-11-05",
                "successful": 2,
                "failed": 0,
                "pdf_downloaded": True,
                "errors": [],
                "report_delivered": False,
            },
        }
        v3 = {"version": 3, **monitor.asdict(monitor.State())}
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


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.config = monitor.TelegramConfig("123:test", "456")
        self.url = "https://api.telegram.org/bot123:test/sendMessage"

    def response(self, body):
        return Response(json.dumps(body).encode("utf-8"), self.url)

    def rate_limit_error(self, parameters):
        body = json.dumps({"parameters": parameters}).encode()
        return HTTPError(self.url, 429, "Rate limited", Message(), BytesIO(body))

    def assert_message_delivered(self):
        try:
            monitor.send_telegram(self.config, "Synthetic notification")
        except monitor.MonitorError as exc:
            self.fail(f"Expected confirmed Telegram delivery, got: {exc}")

    def test_send_posts_json_and_accepts_confirmed_success(self):
        def accept_message(request, **kwargs):
            self.assertEqual(request.full_url, self.url)
            self.assertEqual(request.get_method(), "POST")
            self.assertEqual(request.get_header("Content-type"), "application/json")
            self.assertEqual(
                json.loads(request.data),
                {"chat_id": "456", "text": "Synthetic notification"},
            )
            return self.response({"ok": True})

        with patch("monitor.urlopen", side_effect=accept_message) as opener:
            self.assert_message_delivered()
            opener.assert_called_once()

    def test_only_boolean_true_confirms_delivery(self):
        bodies = ({}, {"ok": False}, {"ok": 0}, {"ok": 1}, {"ok": 1.0}, {"ok": "true"})
        for body in bodies:
            with (
                self.subTest(body=body),
                patch(
                    "monitor.urlopen", side_effect=lambda *a, **kw: self.response(body)
                ) as opener,
                patch("monitor.sleep") as pause,
            ):
                with self.assertRaisesRegex(monitor.MonitorError, "Telegram rejected"):
                    monitor.send_telegram(self.config, "Synthetic notification")
                opener.assert_called_once()
                pause.assert_not_called()

    def test_malformed_responses_fail_after_three_attempts(self):
        bodies = (b"not json", b"[]", b"null", b"true", b'"text"', b"123", b"\xff")
        for body in bodies:
            with (
                self.subTest(body=body),
                patch(
                    "monitor.urlopen",
                    side_effect=lambda *a, **kw: Response(body, self.url),
                ) as opener,
                patch("monitor.sleep") as pause,
            ):
                with self.assertRaisesRegex(
                    monitor.MonitorError, "Telegram returned an invalid response"
                ):
                    monitor.send_telegram(self.config, "Synthetic notification")
                self.assertEqual(opener.call_count, 3)
                self.assertEqual(
                    [call.args[0] for call in pause.call_args_list], [1, 2]
                )

    def test_permanent_failure_is_not_retried_and_does_not_expose_token(self):
        def forbidden(*args, **kwargs):
            raise HTTPError(
                self.url,
                403,
                "Forbidden",
                Message(),
                BytesIO(b'{"description":"123:test"}'),
            )

        with (
            patch("monitor.urlopen", side_effect=forbidden) as opener,
            patch("monitor.sleep") as pause,
        ):
            with self.assertRaises(monitor.MonitorError) as caught:
                monitor.send_telegram(self.config, "Synthetic notification")
            self.assertNotIn(self.config.token, str(caught.exception))
            opener.assert_called_once()
            pause.assert_not_called()

    def test_rate_limit_honors_retry_after(self):
        for retry_after, expected_delay in ((0, 1), (3, 3), (30, 30)):
            responses = [
                self.rate_limit_error({"retry_after": retry_after}),
                self.response({"ok": True}),
            ]
            with (
                self.subTest(retry_after=retry_after),
                patch("monitor.urlopen", side_effect=responses) as opener,
                patch("monitor.sleep") as pause,
            ):
                self.assert_message_delivered()
                self.assertEqual(opener.call_count, 2)
                pause.assert_called_once_with(expected_delay)

    def test_long_rate_limit_defers_delivery_to_a_later_run(self):
        for retry_after in (31, 120):

            def rate_limited(*args, **kwargs):
                raise self.rate_limit_error({"retry_after": retry_after})

            with (
                self.subTest(retry_after=retry_after),
                patch("monitor.urlopen", side_effect=rate_limited) as opener,
                patch("monitor.sleep") as pause,
            ):
                with self.assertRaises(monitor.MonitorError):
                    monitor.send_telegram(self.config, "Synthetic notification")
                opener.assert_called_once()
                pause.assert_not_called()

    def test_invalid_retry_after_uses_fallback_with_bounded_backoff(self):
        parameters = (
            {},
            {"retry_after": -3},
            {"retry_after": "3"},
            {"retry_after": 3.5},
            {"retry_after": True},
            {"retry_after": False},
            {"retry_after": None},
        )
        for value in parameters:
            responses = [
                self.rate_limit_error(value),
                self.rate_limit_error(value),
                self.response({"ok": True}),
            ]
            with (
                self.subTest(parameters=value),
                patch("monitor.urlopen", side_effect=responses) as opener,
                patch("monitor.sleep") as pause,
            ):
                self.assert_message_delivered()
                self.assertEqual(opener.call_count, 3)
                self.assertEqual(
                    [call.args[0] for call in pause.call_args_list], [1, 2]
                )

    def test_api_rate_limit_honors_retry_after(self):
        for retry_after in (3, 30):
            responses = [
                self.response(
                    {
                        "ok": False,
                        "error_code": 429,
                        "parameters": {"retry_after": retry_after},
                    }
                ),
                self.response({"ok": True}),
            ]
            with (
                self.subTest(retry_after=retry_after),
                patch("monitor.urlopen", side_effect=responses) as opener,
                patch("monitor.sleep") as pause,
            ):
                self.assert_message_delivered()
                self.assertEqual(opener.call_count, 2)
                pause.assert_called_once_with(retry_after)

    def test_api_long_rate_limit_defers_delivery_to_a_later_run(self):
        body = {"ok": False, "error_code": 429, "parameters": {"retry_after": 31}}
        with (
            patch(
                "monitor.urlopen", side_effect=lambda *a, **kw: self.response(body)
            ) as opener,
            patch("monitor.sleep") as pause,
        ):
            with self.assertRaises(monitor.MonitorError):
                monitor.send_telegram(self.config, "Synthetic notification")
            opener.assert_called_once()
            pause.assert_not_called()

    def test_api_permanent_and_noninteger_errors_are_not_retried(self):
        for code in (400, 403, "500", 500.0, True, None, [], {}):
            body = {"ok": False, "error_code": code, "description": self.config.token}
            with (
                self.subTest(code=code),
                patch(
                    "monitor.urlopen", side_effect=lambda *a, **kw: self.response(body)
                ) as opener,
                patch("monitor.sleep") as pause,
            ):
                with self.assertRaisesRegex(
                    monitor.MonitorError, "Telegram rejected"
                ) as caught:
                    monitor.send_telegram(self.config, "Synthetic notification")
                self.assertNotIn(self.config.token, str(caught.exception))
                opener.assert_called_once()
                pause.assert_not_called()

    def test_http_temporary_failure_is_retried(self):
        for code in (408, 500, 503):
            responses = [
                HTTPError(self.url, code, "Temporary failure", Message(), BytesIO()),
                self.response({"ok": True}),
            ]
            with (
                self.subTest(code=code),
                patch("monitor.urlopen", side_effect=responses) as opener,
                patch("monitor.sleep") as pause,
            ):
                self.assert_message_delivered()
                self.assertEqual(opener.call_count, 2)
                pause.assert_called_once_with(1)

    def test_api_level_temporary_failure_is_retried(self):
        for code in (408, 500, 503):
            responses = [
                self.response({"ok": False, "error_code": code}),
                self.response({"ok": True}),
            ]
            with (
                self.subTest(code=code),
                patch("monitor.urlopen", side_effect=responses) as opener,
                patch("monitor.sleep") as pause,
            ):
                self.assert_message_delivered()
                self.assertEqual(opener.call_count, 2)
                pause.assert_called_once_with(1)

    def test_network_failure_stops_after_three_total_attempts(self):
        with (
            patch("monitor.urlopen", side_effect=URLError("offline")) as opener,
            patch("monitor.sleep") as pause,
        ):
            with self.assertRaises(monitor.MonitorError):
                monitor.send_telegram(self.config, "Synthetic notification")
            self.assertEqual(opener.call_count, 3)
            self.assertEqual([call.args[0] for call in pause.call_args_list], [1, 2])


if __name__ == "__main__":
    unittest.main()
