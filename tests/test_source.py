import unittest

from support import make_pdf

from visa_pickup_monitor import infrastructure as monitor


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
