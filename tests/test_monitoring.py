import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from email.message import Message
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from support import Response, make_pdf

import monitor as launcher
from visa_pickup_monitor import infrastructure as monitor
from visa_pickup_monitor import monitoring


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
        broadcast=None,
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
        if broadcast is not None:
            settings.update(
                TELEGRAM_BOT_TOKEN=bot_token, TELEGRAM_BROADCAST_CHAT_ID=broadcast
            )
        response_iter = iter(responses)
        request_log = []

        def respond(request, **kwargs):
            request_log.append(request)
            if expected_requests is not None:
                request_index = len(request_log) - 1
                if request_index >= len(expected_requests):
                    self.fail(f"Unexpected HTTP request: {request.full_url}")
                else:
                    expected_url = expected_requests[request_index]
                    self.assertEqual(request.full_url, expected_url)
            if requests is not None:
                requests.append(request)
            response = next(response_iter, None)
            if response is None:
                self.fail(
                    f"No response configured for HTTP request: {request.full_url}"
                )
            if isinstance(response, Exception):
                raise response
            return response

        with (
            patch.dict(os.environ, settings, clear=True),
            patch("visa_pickup_monitor.infrastructure.dotenv_values", return_value={}),
            patch("visa_pickup_monitor.infrastructure.urlopen", side_effect=respond),
            patch(
                "visa_pickup_monitor.infrastructure.sleep",
                side_effect=(lambda delay: sleeps.append(delay))
                if sleeps is not None
                else None,
            ),
            patch("visa_pickup_monitor.monitoring.belgrade_date", return_value=today),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = monitoring.main(self.state_path)
        if expected_requests is not None:
            self.assertEqual(
                len(request_log),
                len(expected_requests),
                "Not all expected HTTP requests were made",
            )
        return result, stdout.getvalue(), stderr.getvalue()

    def test_broadcast_baseline_new_dates_and_daily_cache(self):
        def ok():
            return Response(b'{"ok":true}', "https://api.telegram.org/")

        initial = make_pdf("123456 01.01.2027 08:00-10:00")
        self.assertEqual(
            self.run_script(self.responses(initial), numbers="", broadcast="@news")[0],
            0,
        )
        self.assertTrue(monitor.load_state(self.state_path).broadcast_baseline)
        later = make_pdf(
            "123456 01.01.2027 10:00-11:00\n123456 02.01.2027 08:00-09:00",
            "123456 02.01.2027 10:00-11:00",
        )
        requests = []
        self.assertEqual(
            self.run_script(
                self.responses(later, "new.pdf") + [ok()],
                numbers="",
                broadcast="@news",
                requests=requests,
            )[0],
            0,
        )
        message = json.loads(requests[-1].data)
        self.assertEqual(message["chat_id"], "@news")
        self.assertIn("02.01.2027 08:00-09:00", message["text"])
        self.assertIn("02.01.2027 10:00-11:00", message["text"])
        self.assertNotIn("01.01.2027", message["text"])
        self.assertIn("new.pdf", message["text"])
        self.assertEqual(
            self.run_script(
                [self.responses(later, "new.pdf")[0]], numbers="", broadcast="@news"
            )[0],
            0,
        )
        self.assertEqual(
            self.run_script(
                self.responses(later, "new.pdf"),
                numbers="",
                broadcast="@news",
                today="2026-11-06",
            )[0],
            0,
        )

    def test_broadcast_pending_survives_disable_and_source_failure(self):
        def ok():
            return Response(b'{"ok":true}', "https://api.telegram.org/")

        self.assertEqual(
            self.run_script(
                self.responses(make_pdf("111111 01.01.2027 08:00-09:00")),
                numbers="",
                broadcast="1",
            )[0],
            0,
        )
        rejected = Response(
            b'{"ok":false,"error_code":400}', "https://api.telegram.org/"
        )
        pdf = make_pdf("222222 02.01.2027 08:00-09:00")
        self.assertEqual(
            self.run_script(
                self.responses(pdf, "new.pdf") + [rejected], numbers="", broadcast="1"
            )[0],
            1,
        )
        self.assertEqual(len(monitor.load_state(self.state_path).broadcast_pending), 1)
        with (
            patch(
                "visa_pickup_monitor.monitoring.read_settings",
                return_value={"NUMBERS": "123456"},
            ),
            patch(
                "visa_pickup_monitor.monitoring.fetch",
                side_effect=monitor.MonitorError("offline"),
            ),
        ):
            self.assertEqual(monitoring.main(self.state_path), 1)
        self.assertFalse(monitor.load_state(self.state_path).broadcast_baseline)
        requests = []
        self.assertEqual(
            self.run_script(
                [URLError("offline")] * 3 + [ok()],
                numbers="",
                broadcast="@new",
                requests=requests,
            )[0],
            1,
        )
        self.assertEqual(json.loads(requests[-1].data)["chat_id"], "@new")
        self.assertEqual(monitor.load_state(self.state_path).broadcast_pending, [])
        self.assertFalse(monitor.load_state(self.state_path).broadcast_baseline)
        self.assertEqual(
            self.run_script(self.responses(pdf), numbers="", broadcast="@new")[0], 0
        )

    def test_broadcast_and_personal_delivery_are_independent(self):
        def ok():
            return Response(b'{"ok":true}', "https://api.telegram.org/")

        self.assertEqual(
            self.run_script(
                self.responses(make_pdf("111111 01.01.2027 08:00-09:00")),
                numbers="123456",
                telegram=True,
                broadcast="2",
            )[0],
            0,
        )
        pdf = make_pdf("123456 02.01.2027 08:00-09:00")
        rejected = Response(
            b'{"ok":false,"error_code":400}', "https://api.telegram.org/"
        )
        requests = []
        result = self.run_script(
            self.responses(pdf, "new.pdf") + [rejected, ok(), ok()],
            numbers="123456",
            telegram=True,
            broadcast="2",
            requests=requests,
        )[0]
        self.assertEqual(result, 1)
        messages = [json.loads(request.data) for request in requests if request.data]
        self.assertEqual(
            [message["chat_id"] for message in messages], ["456", "2", "456"]
        )
        self.assertIn("New pickup dates", messages[1]["text"])
        self.assertEqual(monitor.load_state(self.state_path).notified_numbers, [])

    def test_broadcast_failure_does_not_prevent_personal_delivery(self):
        def ok():
            return Response(b'{"ok":true}', "https://api.telegram.org/")

        self.assertEqual(
            self.run_script(
                self.responses(make_pdf("111111 01.01.2027 08:00-09:00")),
                numbers="123456",
                telegram=True,
                broadcast="2",
            )[0],
            0,
        )
        pdf = make_pdf("123456 02.01.2027 08:00-09:00")
        rejected = Response(
            b'{"ok":false,"error_code":400}', "https://api.telegram.org/"
        )
        requests = []
        self.assertEqual(
            self.run_script(
                self.responses(pdf, "new.pdf") + [ok(), rejected, ok()],
                numbers="123456",
                telegram=True,
                broadcast="2",
                requests=requests,
            )[0],
            1,
        )
        messages = [json.loads(request.data) for request in requests if request.data]
        self.assertEqual(
            [message["chat_id"] for message in messages], ["456", "2", "456"]
        )
        state = monitor.load_state(self.state_path)
        self.assertEqual(state.notified_numbers, ["123456"])
        self.assertEqual(len(state.broadcast_pending), 1)

    def test_broadcast_does_not_send_if_saving_discovery_fails(self):
        self.assertEqual(
            self.run_script(
                self.responses(make_pdf("111111 01.01.2027 08:00-09:00")),
                numbers="",
                broadcast="2",
            )[0],
            0,
        )
        before = self.state_path.read_bytes()
        pdf = make_pdf("222222 02.01.2027 08:00-09:00")
        with (
            patch(
                "visa_pickup_monitor.monitoring.save_state",
                side_effect=monitor.MonitorError("disk failure"),
            ),
            patch("visa_pickup_monitor.monitoring.send_telegram") as sender,
        ):
            self.assertEqual(
                self.run_script(
                    self.responses(pdf, "new.pdf"), numbers="", broadcast="2"
                )[0],
                1,
            )
            sender.assert_not_called()
        self.assertEqual(self.state_path.read_bytes(), before)

    def test_broadcast_batches_retry_only_unconfirmed_parts(self):
        def ok():
            return Response(b'{"ok":true}', "https://api.telegram.org/")

        self.assertEqual(
            self.run_script(
                self.responses(make_pdf("111111 01.01.2027 08:00-09:00")),
                numbers="",
                broadcast="1",
            )[0],
            0,
        )
        lines = "\n".join(
            f"{i:06d} 02.01.2027 08:00-09:00" for i in range(200000, 200180)
        )
        rejected = Response(
            b'{"ok":false,"error_code":400}', "https://api.telegram.org/"
        )
        requests = []
        self.assertEqual(
            self.run_script(
                self.responses(make_pdf(lines), "new.pdf") + [ok(), rejected],
                numbers="",
                broadcast="1",
                requests=requests,
            )[0],
            1,
        )
        first = json.loads(requests[-2].data)["text"]
        failed = json.loads(requests[-1].data)["text"]
        self.assertLessEqual(len(first), 4096)
        self.assertLessEqual(len(failed), 4096)
        self.assertEqual(
            monitor.load_state(self.state_path).broadcast_pending, [failed]
        )
        requests = []
        self.assertEqual(
            self.run_script(
                [URLError("offline")] * 3 + [ok()],
                numbers="",
                broadcast="2",
                requests=requests,
            )[0],
            1,
        )
        self.assertEqual(
            json.loads(requests[-1].data), {"chat_id": "2", "text": failed}
        )
        self.assertEqual(monitor.load_state(self.state_path).broadcast_pending, [])

    def test_broadcast_first_baseline_bypasses_personal_cache(self):
        pdf = make_pdf("111111 01.01.2027 08:00-09:00")
        self.assertEqual(self.run_script(self.responses(pdf), numbers="123456")[0], 0)
        self.assertEqual(
            self.run_script(self.responses(pdf), numbers="123456", broadcast="2")[0], 0
        )
        self.assertTrue(monitor.load_state(self.state_path).broadcast_baseline)

    def run_cli(self, cwd, *arguments, env=None):
        return subprocess.run(
            [sys.executable, "-B", str(Path(launcher.__file__).resolve()), *arguments],
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
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
            self.assertEqual(self.run_script(first, telegram=True)[0], 0)
            sender.assert_called_once()
            self.assertIn("Application number 123456", sender.call_args.args[1])
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
            self.assertEqual(self.run_script([], numbers="123456", telegram=True)[0], 0)
            sender.assert_called_once()
            self.assertIn("Application number 123456", sender.call_args.args[1])
            self.assertIn(
                "https://embassy.example/service/current.pdf", sender.call_args.args[1]
            )
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
            "visa_pickup_monitor.monitoring.send_telegram",
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
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
            "visa_pickup_monitor.monitoring.send_telegram",
            side_effect=[None, monitor.MonitorError("Send failed"), None],
        ):
            self.assertEqual(self.run_script([], telegram=True)[0], 1)
        self.assertEqual(
            monitor.load_state(self.state_path).notified_numbers, ["123456"]
        )
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
            self.assertEqual(self.run_script([], telegram=True)[0], 0)
            sender.assert_called_once()
            self.assertIn("000123", sender.call_args.args[1])
        self.assertEqual(monitor.load_state(self.state_path).error_notifications, [])

    def test_confirmed_delivery_survives_interruption_before_the_next_delivery(self):
        pdf = make_pdf("123456 01.01.2020 08:00-10:00\n000123 01.01.2099 08:00-10:00")
        self.assertEqual(self.run_script(self.responses(pdf))[0], 0)
        with patch(
            "visa_pickup_monitor.monitoring.send_telegram",
            side_effect=[None, KeyboardInterrupt()],
        ) as sender:
            with self.assertRaises(KeyboardInterrupt):
                self.run_script([], telegram=True)
            self.assertEqual(sender.call_count, 2)
            self.assertIn("Application number 123456", sender.call_args_list[0].args[1])
            self.assertIn("Application number 000123", sender.call_args_list[1].args[1])
        saved = json.loads(self.state_path.read_text())
        self.assertEqual(saved["notified_numbers"], ["123456"])
        self.assertEqual(saved["error_notifications"], [])
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
            patch(
                "visa_pickup_monitor.infrastructure.os.replace",
                side_effect=fail_notification_save,
            ),
            patch("visa_pickup_monitor.monitoring.send_telegram") as sender,
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
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
            with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
            result, _, _ = self.run_script(
                self.responses(make_pdf("654321 01.01.2099 08:00-10:00")), telegram=True
            )
            self.assertEqual(result, 0)
            sender.assert_not_called()
        self.assertEqual(
            json.loads(self.state_path.read_text())["error_notifications"], []
        )
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
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
                    "visa_pickup_monitor.monitoring.send_telegram",
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
                self.assertTrue(
                    self.state_path.is_file(), "Failed check did not persist state"
                )
                self.assertEqual(
                    json.loads(self.state_path.read_text())["error_notifications"], []
                )
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
            result, _, _ = self.run_script(
                self.responses(make_pdf("654321 01.01.2099 08:00-10:00")), telegram=True
            )
            self.assertEqual(result, 0)
            sender.assert_not_called()

    def test_state_save_failure_prevents_match_delivery_and_reports_the_error(self):
        with (
            patch(
                "visa_pickup_monitor.monitoring.save_state",
                side_effect=monitor.MonitorError("Could not save data/state.json."),
            ),
            patch("visa_pickup_monitor.monitoring.send_telegram") as sender,
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
        with patch("visa_pickup_monitor.monitoring.send_telegram") as sender:
            result, stdout, _ = self.run_script([], telegram=True)
            self.assertEqual(result, 1)
            self.assertEqual(stdout, "")
            sender.assert_called_once()
        self.assertEqual(self.state_path.read_text(), "broken state")

    def test_invalid_cached_targets_fail_without_changing_state_or_requesting_source(
        self,
    ):
        state = {
            "version": 4,
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

    def test_legacy_state_is_rejected_without_changing_history_or_requesting_source(
        self,
    ):
        for version in (1, 2, 3):
            for numbers in ("123456", "123456,000123"):
                with (
                    self.subTest(version=version, numbers=numbers),
                    patch("visa_pickup_monitor.monitoring.send_telegram") as sender,
                ):
                    self.write_legacy_state(version)
                    original = self.state_path.read_bytes()
                    result, stdout, stderr = self.run_script(
                        [], numbers=numbers, telegram=True, expected_requests=[]
                    )
                    self.assertEqual(result, 1)
                    self.assertEqual(stdout, "")
                    self.assertIn(
                        "Invalid state; restore data/state.json before retrying.",
                        stderr,
                    )
                    self.assertNotIn("Traceback", stderr)
                    self.assertEqual(self.state_path.read_bytes(), original)
                    sender.assert_called_once()
                    self.assertIn("Monitor error", sender.call_args.args[1])

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
