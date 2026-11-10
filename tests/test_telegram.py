import json
import unittest
from email.message import Message
from io import BytesIO
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from support import Response

from visa_pickup_monitor import infrastructure as monitor


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

        with patch(
            "visa_pickup_monitor.infrastructure.urlopen", side_effect=accept_message
        ) as opener:
            self.assert_message_delivered()
            opener.assert_called_once()

    def test_only_boolean_true_confirms_delivery(self):
        bodies = ({}, {"ok": False}, {"ok": 0}, {"ok": 1}, {"ok": 1.0}, {"ok": "true"})
        for body in bodies:
            with (
                self.subTest(body=body),
                patch(
                    "visa_pickup_monitor.infrastructure.urlopen",
                    side_effect=lambda *a, **kw: self.response(body),
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
                    "visa_pickup_monitor.infrastructure.urlopen",
                    side_effect=lambda *a, **kw: Response(body, self.url),
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
            patch(
                "visa_pickup_monitor.infrastructure.urlopen", side_effect=forbidden
            ) as opener,
            patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
                patch(
                    "visa_pickup_monitor.infrastructure.urlopen", side_effect=responses
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
                patch(
                    "visa_pickup_monitor.infrastructure.urlopen",
                    side_effect=rate_limited,
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
                patch(
                    "visa_pickup_monitor.infrastructure.urlopen", side_effect=responses
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
                patch(
                    "visa_pickup_monitor.infrastructure.urlopen", side_effect=responses
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
            ):
                self.assert_message_delivered()
                self.assertEqual(opener.call_count, 2)
                pause.assert_called_once_with(retry_after)

    def test_api_long_rate_limit_defers_delivery_to_a_later_run(self):
        body = {"ok": False, "error_code": 429, "parameters": {"retry_after": 31}}
        with (
            patch(
                "visa_pickup_monitor.infrastructure.urlopen",
                side_effect=lambda *a, **kw: self.response(body),
            ) as opener,
            patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
                    "visa_pickup_monitor.infrastructure.urlopen",
                    side_effect=lambda *a, **kw: self.response(body),
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
                patch(
                    "visa_pickup_monitor.infrastructure.urlopen", side_effect=responses
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
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
                patch(
                    "visa_pickup_monitor.infrastructure.urlopen", side_effect=responses
                ) as opener,
                patch("visa_pickup_monitor.infrastructure.sleep") as pause,
            ):
                self.assert_message_delivered()
                self.assertEqual(opener.call_count, 2)
                pause.assert_called_once_with(1)

    def test_network_failure_stops_after_three_total_attempts(self):
        with (
            patch(
                "visa_pickup_monitor.infrastructure.urlopen",
                side_effect=URLError("offline"),
            ) as opener,
            patch("visa_pickup_monitor.infrastructure.sleep") as pause,
        ):
            with self.assertRaises(monitor.MonitorError):
                monitor.send_telegram(self.config, "Synthetic notification")
            self.assertEqual(opener.call_count, 3)
            self.assertEqual([call.args[0] for call in pause.call_args_list], [1, 2])
