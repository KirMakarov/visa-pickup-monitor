"""Check the German Embassy in Belgrade's current visa pickup list once."""

import os
import re
import sys
from datetime import datetime
from html.parser import HTMLParser
from http.client import HTTPException
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlsplit
from urllib.request import Request, urlopen

from dotenv import dotenv_values
from pypdf import PdfReader

DEFAULT_PAGE_URL = "https://belgrad.diplo.de/rs-sr/service/2339474-2339474"
ENTRY_PATTERN = re.compile(
    r"(?<!\w)([0-9]{6})\s+([0-9]{2}\.[0-9]{2}\.[0-9]{4})\s+"
    r"([0-9]{2}:[0-9]{2})\s*-\s*([0-9]{2}:[0-9]{2})(?![\w:])"
)


class MonitorError(Exception):
    """A configuration, download, or pickup-list processing failure."""


def load_config(env_path: str | Path = ".env") -> tuple[str, tuple[str, ...]]:
    try:
        settings = {**dotenv_values(env_path), **os.environ}
    except (OSError, UnicodeError) as exc:
        raise MonitorError("Could not read the .env file.") from exc

    page_url = (settings.get("PAGE_URL") or DEFAULT_PAGE_URL).strip()
    try:
        parsed_url = urlsplit(page_url)
    except ValueError as exc:
        raise MonitorError("PAGE_URL must be an absolute HTTP or HTTPS URL.") from exc
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise MonitorError("PAGE_URL must be an absolute HTTP or HTTPS URL.")

    numbers = tuple(number.strip() for number in (settings.get("NUMBERS") or "").split(","))
    if any(re.fullmatch(r"[0-9]{6}", number) is None for number in numbers):
        raise MonitorError("Set NUMBERS to comma-separated six-digit application numbers.")
    return page_url, tuple(dict.fromkeys(numbers))


class _PickupLinkParser(HTMLParser):
    def __init__(self, page_url: str):
        super().__init__()
        self.page_url = page_url
        self.links: set[str] = set()
        self.href: str | None = None
        self.label: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data):
        if self.href is not None:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or self.href is None:
            return
        label = " ".join(" ".join(self.label).casefold().split())
        url = urldefrag(urljoin(self.page_url, self.href))[0]
        parts = urlsplit(url)
        if (
            parts.scheme in {"http", "https"}
            and parts.path.lower().endswith(".pdf")
            and ("abholliste" in label or "lista za preuzimanje viza" in label)
        ):
            self.links.add(url)
        self.href = None


def resolve_pdf_url(html: str, page_url: str) -> str:
    parser = _PickupLinkParser(page_url)
    try:
        parser.feed(html)
        parser.close()
    except ValueError as exc:
        raise MonitorError("Could not parse the source page's pickup-list links.") from exc
    if len(parser.links) != 1:
        raise MonitorError(
            f"Expected one pickup-list PDF link; found {len(parser.links)}. "
            "Check the source page."
        )
    return next(iter(parser.links))


def fetch(url: str, label: str) -> tuple[bytes, str]:
    request = Request(url, headers={"User-Agent": "visa-pickup-monitor/0.1"})
    try:
        with urlopen(request, timeout=30) as response:
            return response.read(), response.geturl()
    except HTTPError as exc:
        exc.close()
        raise MonitorError(f"Could not download {label} (HTTP {exc.code}).") from exc
    except (URLError, OSError, HTTPException, ValueError) as exc:
        raise MonitorError(f"Could not download {label}; check the connection and URL.") from exc


def parse_numbers(text: str) -> set[str]:
    numbers = set()
    for number, date, start, end in ENTRY_PATTERN.findall(text):
        try:
            datetime.strptime(date, "%d.%m.%Y")
            datetime.strptime(start, "%H:%M")
            datetime.strptime(end, "%H:%M")
        except ValueError:
            continue
        numbers.add(number)
    return numbers


def extract_numbers(pdf: bytes) -> set[str]:
    try:
        texts = [page.extract_text() or "" for page in PdfReader(BytesIO(pdf)).pages]
    except Exception as exc:
        raise MonitorError("Could not read the PDF or extract its text.") from exc

    numbers = set()
    for text in texts:
        numbers.update(parse_numbers(text))
    if not numbers:
        raise MonitorError("The PDF contains no valid pickup entries; check its format.")
    return numbers


def main() -> int:
    try:
        page_url, targets = load_config()
        page, effective_page_url = fetch(page_url, "the source page")
        try:
            html = page.decode("utf-8")
        except UnicodeError as exc:
            raise MonitorError("The source page is not valid UTF-8 text.") from exc
        pdf_url = resolve_pdf_url(html, effective_page_url)
        pdf, pdf_url = fetch(pdf_url, "the pickup-list PDF")
        numbers = extract_numbers(pdf)
    except MonitorError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"PDF: {pdf_url}")
    print(f"Valid application numbers: {len(numbers)}")
    for target in targets:
        result = "found in pickup list" if target in numbers else "not found in pickup list"
        print(f"{target}: {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
