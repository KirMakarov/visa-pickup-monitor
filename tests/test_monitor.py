import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
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
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
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
            page_url, numbers = monitor.load_config(self.env_path)
        self.assertEqual(page_url, monitor.DEFAULT_PAGE_URL)
        self.assertEqual(numbers, ("000123", "123456"))

    def test_environment_overrides_dotenv(self):
        self.env_path.write_text(
            "PAGE_URL=https://file.example/page\nNUMBERS=123456\n", encoding="utf-8"
        )
        settings = {"PAGE_URL": "https://env.example/page", "NUMBERS": "000123"}
        with patch.dict(os.environ, settings, clear=True):
            self.assertEqual(
                monitor.load_config(self.env_path),
                ("https://env.example/page", ("000123",)),
            )

    def test_missing_or_invalid_numbers_are_configuration_errors(self):
        for value in ("", "12345", "1234567", "abc123", "123456,", "１２３４５６"):
            with self.subTest(value=value), patch.dict(os.environ, {"NUMBERS": value}, clear=True):
                with self.assertRaisesRegex(monitor.MonitorError, "NUMBERS"):
                    monitor.load_config(self.env_path)

    def test_invalid_source_urls_are_configuration_errors(self):
        for url in ("file:///tmp/page.html", "embassy.example/page", "https://[invalid"):
            settings = {"NUMBERS": "123456", "PAGE_URL": url}
            with self.subTest(url=url), patch.dict(os.environ, settings, clear=True):
                with self.assertRaisesRegex(monitor.MonitorError, "PAGE_URL"):
                    monitor.load_config(self.env_path)


class LinkTests(unittest.TestCase):
    def test_resolves_named_relative_pdf_and_ignores_unrelated_documents(self):
        html = '''
            <a href="authorization.pdf">Authorization</a>
            <a href="../files/current.pdf?download=1#page=2">
                <span>Abholliste/ Lista za preuzimanje viza</span> PDF
            </a>
            <a href="../files/current.pdf?download=1">Abholliste</a>
        '''
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
        text = (
            "123456 31.02.2026 08:00-10:00\n"
            "123456 10.11.2026 25:00-26:00\n"
        )
        self.assertEqual(monitor.parse_numbers(text), set())

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
            with self.subTest(size=len(pdf)), self.assertRaisesRegex(
                monitor.MonitorError, "no valid pickup entries"
            ):
                monitor.extract_numbers(pdf)


class Response(BytesIO):
    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self.url = url

    def geturl(self):
        return self.url


class ScriptTests(unittest.TestCase):
    def run_script(self, responses, numbers="123456,000123"):
        stdout, stderr = StringIO(), StringIO()
        settings = {"PAGE_URL": "https://embassy.example/start", "NUMBERS": numbers}
        with (
            patch.dict(os.environ, settings, clear=True),
            patch("monitor.dotenv_values", return_value={}),
            patch("monitor.urlopen", side_effect=responses),
            redirect_stdout(stdout),
            redirect_stderr(stderr),
        ):
            result = monitor.main()
        return result, stdout.getvalue(), stderr.getvalue()

    def responses(self, pdf):
        html = b'<a href="current.pdf">Abholliste</a>'
        return [
            Response(html, "https://embassy.example/service/"),
            Response(pdf, "https://embassy.example/service/current.pdf"),
        ]

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
        self.assertEqual(stderr, "")

    def test_download_failures_return_error_without_false_absence_results(self):
        failures = (
            URLError("offline"),
            HTTPError("https://embassy.example/start", 503, "Unavailable", {}, None),
            TimeoutError("timed out"),
        )
        for failure in failures:
            with self.subTest(failure=type(failure).__name__):
                result, stdout, stderr = self.run_script([failure])
                self.assertEqual(result, 1)
                self.assertEqual(stdout, "")
                self.assertIn("Error: Could not download the source page", stderr)
                self.assertNotIn("Traceback", stderr)

    def test_unrecognized_pdf_returns_error_without_false_absence_results(self):
        result, stdout, stderr = self.run_script(self.responses(make_pdf("No pickup entries")))
        self.assertEqual(result, 1)
        self.assertEqual(stdout, "")
        self.assertIn("no valid pickup entries", stderr)


if __name__ == "__main__":
    unittest.main()
